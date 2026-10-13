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
# Optional on/off phases (shared wall-clock anchor SS_T0, so a whole application
# switches together) and a duty cycle (busy share of each step + sleep), used by
# the blame study to build culprits and decoys whose usage differs over time.
on_s = float(E.get('SS_ON_S', '0')); off_s = float(E.get('SS_OFF_S', '0')); t_anchor = float(E.get('SS_T0', str(time.time())))
phase = float(E.get('SS_PHASE_S', '0')); duty = min(1.0, max(0.01, float(E.get('SS_DUTY', '1'))))
def on(now): return on_s <= 0 or off_s <= 0 or ((now - t_anchor + phase) % (on_s + off_s)) < on_s
# Run until the parent signals the shared stop event (bounded by a safety cap).
# Completed units are bucketed by 100 ms of wall time so the parent can count
# exactly the work done inside the foreground measurement window.
start = time.perf_counter(); end = start + max_duration; units = 0; buckets = {}
while time.perf_counter() < end and _k.WaitForSingleObject(stop_event, 0) != 0:
    if not on(time.time()): time.sleep(0.02); continue
    t_step = time.perf_counter(); step(); units += 1; bucket = int(time.time() * 10); buckets[bucket] = buckets.get(bucket, 0) + 1
    if duty < 1: time.sleep((time.perf_counter() - t_step) * (1 - duty) / duty)
print(json.dumps({'role': role, 'units': units, 'elapsed_s': time.perf_counter() - start, 'buckets': buckets}), flush=True)
'''

# An "application": a launcher process that starts SS_CHILDREN copies of the
# program in SS_CHILD_CODE and waits for them, so blame has a real process tree
# to find. SS_APP_PRIORITY=idle lowers the launcher before spawning; children
# inherit IDLE priority (a heavy but polite application, the classic decoy).
LAUNCHER = r"""
import json, os, subprocess, sys, ctypes
E = os.environ
if E.get('SS_APP_PRIORITY') == 'idle':
    k = ctypes.WinDLL('kernel32'); k.GetCurrentProcess.restype = ctypes.c_void_p; k.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    k.SetPriorityClass(k.GetCurrentProcess(), 0x40)
code = E['SS_CHILD_CODE']; env = {k: v for k, v in E.items() if k != 'SS_CHILD_CODE'}
kids = [subprocess.Popen([sys.executable, '-c', code], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(int(E.get('SS_CHILDREN', '1')))]
for kid in kids:
    if kid.stdout.readline().strip() != 'READY': raise SystemExit('child failed: ' + kid.stderr.read()[-300:])
print('READY', flush=True)
units = 0; buckets = {}
for kid in kids:
    out, _ = kid.communicate()
    for line in out.strip().splitlines()[-1:]:
        r = json.loads(line); units += r['units']
        for b, n in r['buckets'].items(): buckets[b] = buckets.get(b, 0) + n
print(json.dumps({'role': E.get('SS_ROLE', '?'), 'units': units, 'buckets': buckets, 'children': len(kids)}), flush=True)
"""

# A real tool run in a loop (a build, a hash, an archive, a search) until the
# shared stop event is set. Work done is the CPU time of the tool's process
# tree, bucketed per 100 ms (units = CPU milliseconds), because real tools do
# not report progress in comparable units. I/O bytes are bucketed alongside.
LOOPER = _UNBUFFERED + r"""
import json, subprocess, sys, time, psutil
E = os.environ
_k.OpenEventW.restype = wintypes.HANDLE; _k.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
_k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
stop_event = _k.OpenEventW(0x00100000, False, E['SS_STOP_EVENT'])
cmd = json.loads(E['SS_CMD']); cwd = E.get('SS_CWD') or None; end = time.time() + float(E.get('SS_BG_MAX_DURATION', '120'))
def stopped(): return _k.WaitForSingleObject(stop_event, 0) == 0
def tree(p):
    try: return [p] + p.children(recursive=True)
    except psutil.Error: return [p]
seen = {}
def sample(procs):
    cpu = io = 0.0
    for q in procs:
        try:
            t = q.cpu_times(); c = q.io_counters(); key = (q.pid, q.create_time())
            seen[key] = (t.user + t.system, c.read_bytes + c.write_bytes)
        except psutil.Error: pass
    return sum(v[0] for v in seen.values()), sum(v[1] for v in seen.values())
runs = 0; buckets = {}; io_buckets = {}; last = (0.0, 0.0); child = None
print('READY', flush=True)
while not stopped() and time.time() < end:
    if child is None or child.poll() is not None:
        if child is not None: runs += 1
        child = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); proc = psutil.Process(child.pid)
    now = sample(tree(proc)); b = int(time.time() * 10)
    buckets[b] = buckets.get(b, 0) + round((now[0] - last[0]) * 1000); io_buckets[b] = io_buckets.get(b, 0) + round((now[1] - last[1]) / 1e6, 3); last = now
    time.sleep(0.1)
if child is not None and child.poll() is None:
    for q in reversed(tree(psutil.Process(child.pid))):
        try: q.kill()
        except psutil.Error: pass
print(json.dumps({'role': E.get('SS_ROLE', 'real'), 'units': sum(buckets.values()), 'runs': runs, 'buckets': buckets, 'io_mb_buckets': io_buckets}), flush=True)
"""

# Responsiveness of a REAL, unmodified application: round-trip time of WM_NULL
# sent with SendMessageTimeout to its top-level window. The reply comes from the
# application's own UI thread, so the time is how long its message pump took to
# get scheduled and answer, the same test Windows uses to mark a window
# "Not Responding". Rows keep the probe's columns; `ui` is the app's latency.
UIPROBE = r"""
import ctypes, json, os, sys, time
from ctypes import wintypes
import psutil
E = os.environ
u = ctypes.WinDLL('user32', use_last_error=True)
u.SendMessageTimeoutW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM, wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]
u.SendMessageTimeoutW.restype = ctypes.c_size_t
u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
u.IsWindowVisible.argtypes = [wintypes.HWND]; u.GetWindowTextLengthW.argtypes = [wintypes.HWND]
target = E['SS_UI_EXE'].lower(); interval = float(E.get('SS_INTERVAL', '0.05')); warmup = float(E.get('SS_WARMUP', '1.0'))
duration = float(E.get('SS_DURATION', '6.0')); timeout_ms = int(E.get('SS_UI_TIMEOUT_MS', '1000')); not_before = float(E.get('SS_UI_NOT_BEFORE', '0'))
def find():
    hits = []
    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        if u.IsWindowVisible(hwnd) and u.GetWindowTextLengthW(hwnd) > 0:
            pid = wintypes.DWORD(); u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            try:
                p = psutil.Process(pid.value)
                if p.name().lower() == target and p.create_time() >= not_before: hits.append((p.create_time(), hwnd, pid.value))
            except psutil.Error: pass
        return True
    u.EnumWindows(cb, 0)
    return max(hits) if hits else None
give_up = time.time() + 15; hit = None
while hit is None and time.time() < give_up:
    hit = find()
    if hit is None: time.sleep(0.2)
if hit is None: raise SystemExit('no visible window for ' + target)
_, hwnd, pid = hit; res = ctypes.c_size_t()
def beat():
    t0 = time.perf_counter(); ok = u.SendMessageTimeoutW(hwnd, 0, 0, 0, 0, timeout_ms, ctypes.byref(res))
    return (time.perf_counter() - t0) * 1000, bool(ok)
end_warm = time.perf_counter() + warmup
while time.perf_counter() < end_warm: beat(); time.sleep(interval)
start = time.perf_counter(); wall_start = time.time(); deadline = start + interval; rows = []; stop = start + duration
while deadline <= stop:
    remaining = deadline - time.perf_counter()
    if remaining > 0: time.sleep(remaining)
    actual = time.perf_counter(); delay = max(0.0, actual - deadline) * 1000
    rtt, ok = beat()
    rows.append({'t': round((actual - start) * 1000, 3), 'sched': round(delay, 4), 'cpu': 0.0, 'mem': 0.0, 'io': 0.0, 'ui': round(rtt, 4), 'timeout': not ok})
    deadline += interval
    while deadline < time.perf_counter(): deadline += interval
print(json.dumps({'rows': rows, 'duration_ms': (time.perf_counter() - start) * 1000, 'wall_start': wall_start, 'wall_end': time.time(), 'ui_pid': pid}), flush=True)
"""


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
