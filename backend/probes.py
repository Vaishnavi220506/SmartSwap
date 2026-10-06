"""Child-process programs for the foreground probe, the canary, and background load.

These run as `python -c <source>`. They deliberately avoid `multiprocessing`:
on Windows the spawn start method cannot re-import functions defined in a `-c`
script, which silently killed every CPU worker in SmartSwap v1.

Each probe beat has three timed components so interference can be attributed:
  compute  - a fixed interpreter loop (CPU time-slice sensitive)
  memory   - a streaming copy of a buffer larger than L2 (bandwidth/LLC sensitive)
  io       - one 4 KiB unbuffered random read (storage-queue sensitive)
"""
from __future__ import annotations

import os
from pathlib import Path

DATA_FILE_MB = 1024
PROBE_FILE_MB = 64

# Shared helpers for unbuffered (FILE_FLAG_NO_BUFFERING) reads via Win32.
_UNBUFFERED = r'''
import ctypes, os
from ctypes import wintypes
_k = ctypes.WinDLL('kernel32', use_last_error=True)
_k.CreateFileW.restype = wintypes.HANDLE
_k.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
_k.ReadFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
_k.SetFilePointerEx.argtypes = [wintypes.HANDLE, ctypes.c_longlong, ctypes.POINTER(ctypes.c_longlong), wintypes.DWORD]
_k.VirtualAlloc.restype = wintypes.LPVOID
_k.VirtualAlloc.argtypes = [wintypes.LPVOID, ctypes.c_size_t, wintypes.DWORD, wintypes.DWORD]
class Unbuffered:
    def __init__(self, path, block):
        self.h = _k.CreateFileW(path, 0x80000000, 1, None, 3, 0x20000000, None)
        if self.h in (None, wintypes.HANDLE(-1).value): raise OSError(ctypes.get_last_error(), path)
        self.block = block; self.buf = _k.VirtualAlloc(None, block, 0x3000, 0x04)
        self.blocks = os.path.getsize(path) // block; self.got = wintypes.DWORD()
    def read(self, index):
        _k.SetFilePointerEx(self.h, (index % self.blocks) * self.block, None, 0)
        _k.ReadFile(self.h, self.buf, self.block, ctypes.byref(self.got), None)
        return self.got.value
'''

# Foreground probe and canary share one program; the role only changes cadence,
# output streaming, and work sizes (set by the parent through env vars).
PROBE = _UNBUFFERED + r'''
import json, sys, time
E = os.environ
interval = float(E.get('SS_INTERVAL', '0.05')); warmup = float(E.get('SS_WARMUP', '1.0'))
duration = float(E.get('SS_DURATION', '6.0')); stream = E.get('SS_STREAM') == '1'
iters = int(E.get('SS_COMPUTE_ITERS', '12000')); mem_bytes = int(E.get('SS_MEM_BYTES', str(8 << 20)))
io_path = E.get('SS_IO_FILE', ''); seed = int(E.get('SS_SEED', '17'))
src = bytearray(mem_bytes); dst = bytearray(mem_bytes)
for i in range(0, mem_bytes, 4096): src[i] = i & 255
reader = Unbuffered(io_path, 4096) if io_path else None
rng = seed
def beat():
    global rng
    t0 = time.perf_counter(); v = rng
    for i in range(iters): v = (v * 1664525 + i + 1013904223) & 0xffffffff
    t1 = time.perf_counter(); dst[:] = src
    t2 = time.perf_counter()
    if reader: reader.read(v >> 7)
    t3 = time.perf_counter(); rng = v
    return (t1 - t0) * 1000, (t2 - t1) * 1000, (t3 - t2) * 1000
end_warm = time.perf_counter() + warmup
while time.perf_counter() < end_warm: beat(); time.sleep(interval)
if stream: print('READY', flush=True)
start = time.perf_counter(); wall_start = time.time(); deadline = start + interval; rows = []
stop = (start + duration) if duration > 0 else float('inf')
while deadline <= stop:
    remaining = deadline - time.perf_counter()
    if remaining > 0: time.sleep(remaining)
    actual = time.perf_counter(); delay = max(0.0, actual - deadline) * 1000
    c, m, io_ms = beat()
    row = {'t': round((actual - start) * 1000, 3), 'sched': round(delay, 4), 'cpu': round(c, 4), 'mem': round(m, 4), 'io': round(io_ms, 4)}
    if stream: sys.stdout.write(json.dumps(row) + '\n'); sys.stdout.flush()
    else: rows.append(row)
    deadline += interval
    while deadline < time.perf_counter(): deadline += interval  # skip missed slots rather than bursting
if not stream: print(json.dumps({'rows': rows, 'duration_ms': (time.perf_counter() - start) * 1000, 'wall_start': wall_start, 'wall_end': time.time()}), flush=True)
'''

# One background worker process. Role selects the resource it saturates. It
# counts completed work units so the cost of throttling it can be measured.
WORKER = _UNBUFFERED + r'''
import json, random, sys, time
E = os.environ
role = E['SS_ROLE']; max_duration = float(E.get('SS_BG_MAX_DURATION', '60'))
_k.OpenEventW.restype = wintypes.HANDLE; _k.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
_k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
stop_event = _k.OpenEventW(0x00100000, False, E['SS_STOP_EVENT'])  # SYNCHRONIZE
if not stop_event: raise SystemExit('stop event missing')
if role == 'cpu':
    def step():
        x = 17
        for i in range(20000): x = (x * 1664525 + i + 1013904223) & 0xffffffff
elif role == 'membw':
    size = int(E.get('SS_BG_MEM_BYTES', str(64 << 20)))
    src = bytearray(size); dst = bytearray(size)
    for i in range(0, size, 4096): src[i] = 1
    def step(): dst[:] = src
elif role == 'io':
    reader = Unbuffered(E['SS_IO_FILE'], int(E.get('SS_BG_IO_BLOCK', '65536'))); r = random.Random(int(E.get('SS_SEED', '3')))
    def step(): reader.read(r.randrange(1 << 30))
else:
    raise SystemExit('unknown role ' + role)
step()
print('READY', flush=True)
# Run until the parent signals the shared stop event (bounded by a safety cap).
# Completed units are bucketed by 100 ms of wall time so the parent can count
# exactly the work done inside the foreground measurement window.
start = time.perf_counter(); end = start + max_duration; units = 0; buckets = {}
while time.perf_counter() < end and _k.WaitForSingleObject(stop_event, 0) != 0:
    step(); units += 1; bucket = int(time.time() * 10); buckets[bucket] = buckets.get(bucket, 0) + 1
print(json.dumps({'role': role, 'units': units, 'elapsed_s': time.perf_counter() - start, 'buckets': buckets}), flush=True)
'''


def ensure_data_files(directory: Path) -> tuple[Path, Path]:
    """Create the read-only I/O datasets once (incompressible bytes, written one time)."""
    directory.mkdir(parents=True, exist_ok=True)
    out = []
    for name, size_mb in (("bg-read-dataset.bin", DATA_FILE_MB), ("probe-read-dataset.bin", PROBE_FILE_MB)):
        path = directory / name
        if not path.exists() or path.stat().st_size != size_mb << 20:
            with open(path, "wb") as handle:
                for _ in range(size_mb):
                    handle.write(os.urandom(1 << 20))
        out.append(path)
    return out[0], out[1]
