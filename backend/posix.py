"""Linux port of SmartSwap's probes and controls: the cross-OS validation path.

Windows has no kernel stall metric, so on Windows the canary's WSI can only be
checked against application latency. Linux has one (PSI, /proc/pressure), so
running the *same* unprivileged canary, controller, and blame logic on Linux
lets the WSI be compared with a kernel ground truth. The controller and blame
modules are reused unchanged; only probes and actuators differ:

    Windows                         Linux
    IDLE priority class        ->   nice 19
    very-low I/O priority      ->   ioprio class IDLE
    Job Object CPU-rate cap    ->   cgroup CPU quota (v1 cfs_quota_us / v2 cpu.max)
    FILE_FLAG_NO_BUFFERING     ->   O_DIRECT

Every setter reads the kernel state back, like native.py. Workers stop when a
stop file appears (the analogue of the named event).
"""
from __future__ import annotations

import ctypes
import os
import platform
from pathlib import Path

IS_LINUX = os.uname().sysname == "Linux" if hasattr(os, "uname") else False
NICE_IDLE = 19
IOPRIO_CLASS_IDLE = 3
_IOPRIO_SYSCALLS = {"x86_64": (251, 252), "aarch64": (30, 31)}  # (set, get)

# O_DIRECT 4 KiB reads into a page-aligned mmap buffer.
_DIRECT = r'''
import mmap, os
class Direct:
    def __init__(self, path, block):
        self.fd = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECT', 0)); self.block = block
        self.buf = mmap.mmap(-1, block); self.blocks = os.fstat(self.fd).st_size // block
    def read(self, index):
        return os.preadv(self.fd, [self.buf], (index % self.blocks) * self.block)
'''

PROBE = _DIRECT + r'''
import json, sys, time
E = os.environ
interval = float(E.get('SS_INTERVAL', '0.05')); warmup = float(E.get('SS_WARMUP', '1.0'))
duration = float(E.get('SS_DURATION', '6.0')); stream = E.get('SS_STREAM') == '1'
iters = int(E.get('SS_COMPUTE_ITERS', '12000')); mem_bytes = int(E.get('SS_MEM_BYTES', str(8 << 20)))
io_path = E.get('SS_IO_FILE', ''); rng = int(E.get('SS_SEED', '17'))
src = bytearray(mem_bytes); dst = bytearray(mem_bytes)
for i in range(0, mem_bytes, 4096): src[i] = i & 255
reader = Direct(io_path, 4096) if io_path else None
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
    while deadline < time.perf_counter(): deadline += interval
if not stream: print(json.dumps({'rows': rows, 'duration_ms': (time.perf_counter() - start) * 1000, 'wall_start': wall_start, 'wall_end': time.time()}), flush=True)
'''

WORKER = _DIRECT + r'''
import json, random, sys, time
E = os.environ
role = E['SS_ROLE']; stop_file = E['SS_STOP_FILE']; end = time.time() + float(E.get('SS_BG_MAX_DURATION', '60'))
if role == 'cpu':
    def step():
        x = 17
        for i in range(20000): x = (x * 1664525 + i + 1013904223) & 0xffffffff
elif role == 'membw':
    size = int(E.get('SS_BG_MEM_BYTES', str(64 << 20))); src = bytearray(size); dst = bytearray(size)
    for i in range(0, size, 4096): src[i] = 1
    def step(): dst[:] = src
elif role == 'io':
    reader = Direct(E['SS_IO_FILE'], int(E.get('SS_BG_IO_BLOCK', str(1 << 20)))); r = random.Random(int(E.get('SS_SEED', '3')))
    def step(): reader.read(r.randrange(1 << 30))
else:
    raise SystemExit('unknown role ' + role)
step(); print('READY', flush=True)
on_s = float(E.get('SS_ON_S', '0')); off_s = float(E.get('SS_OFF_S', '0')); t_anchor = float(E.get('SS_T0', str(time.time())))
duty = min(1.0, max(0.01, float(E.get('SS_DUTY', '1'))))
def on(now): return on_s <= 0 or off_s <= 0 or ((now - t_anchor) % (on_s + off_s)) < on_s
units = 0; buckets = {}; checks = 0
while time.time() < end:
    checks += 1
    if checks % 4 == 0 and os.path.exists(stop_file): break
    if not on(time.time()): time.sleep(0.02); continue
    t_step = time.perf_counter(); step(); units += 1; b = int(time.time() * 10); buckets[b] = buckets.get(b, 0) + 1
    if duty < 1: time.sleep((time.perf_counter() - t_step) * (1 - duty) / duty)
print(json.dumps({'role': role, 'units': units, 'buckets': buckets}), flush=True)
'''

LAUNCHER = r'''
import json, os, subprocess, sys
E = os.environ
if E.get('SS_APP_PRIORITY') == 'idle': os.nice(19)
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
'''


# --- controls ---------------------------------------------------------------
def get_nice(pid: int) -> int | None:
    try: return os.getpriority(os.PRIO_PROCESS, pid)
    except OSError: return None


def set_nice(pid: int, value: int) -> bool:
    try: os.setpriority(os.PRIO_PROCESS, pid, value)
    except OSError: return False
    return get_nice(pid) == value


def _ioprio(nr: int, *args: int) -> int:
    libc = ctypes.CDLL(None, use_errno=True)
    return libc.syscall(nr, *[ctypes.c_long(a) for a in args])


def get_ioprio(pid: int) -> int | None:
    nrs = _IOPRIO_SYSCALLS.get(platform.machine())
    if not nrs: return None
    value = _ioprio(nrs[1], 1, pid)  # IOPRIO_WHO_PROCESS
    return None if value < 0 else value


def set_ioprio(pid: int, value: int) -> bool:
    nrs = _IOPRIO_SYSCALLS.get(platform.machine())
    if not nrs: return False
    return _ioprio(nrs[0], 1, pid, value) == 0 and get_ioprio(pid) == value


def ioprio_idle() -> int:
    return IOPRIO_CLASS_IDLE << 13


def psi_totals() -> dict[str, float]:
    """Cumulative 'some' stall microseconds per resource from /proc/pressure (empty if unsupported)."""
    out = {}
    for resource, name in (("cpu", "cpu"), ("io", "io"), ("mem", "memory")):
        try:
            for line in Path(f"/proc/pressure/{name}").read_text().splitlines():
                if line.startswith("some"): out[resource] = float(line.rsplit("total=", 1)[1])
        except (OSError, ValueError, IndexError): continue
    return out


class CpuCap:
    """A SmartSwap-owned cgroup with a hard CPU quota (the Job Object analogue). Needs a writable cgroup."""

    def __init__(self, name: str) -> None:
        self.v2 = Path("/sys/fs/cgroup/cgroup.controllers").exists() and not Path("/sys/fs/cgroup/cpu").exists()
        base = Path("/sys/fs/cgroup") if self.v2 else Path("/sys/fs/cgroup/cpu")
        self.base = base; self.path = base / name; self.members: set[int] = set(); self.percent: float | None = None
        try: self.path.mkdir(exist_ok=True); self.ok = True
        except OSError: self.ok = False

    def assign(self, pid: int) -> bool:
        if not self.ok: return False
        try: (self.path / "cgroup.procs").write_text(str(pid))
        except OSError: return False
        ok = str(pid) in (self.path / "cgroup.procs").read_text().split()
        if ok: self.members.add(pid)
        return ok

    def _period(self) -> int:
        if self.v2: return int((self.path / "cpu.max").read_text().split()[1])
        return int((self.path / "cpu.cfs_period_us").read_text())

    def set_percent(self, percent: float) -> bool:
        """Cap the group to `percent` of total machine CPU; verified by read-back."""
        if not self.ok: return False
        period = self._period(); quota = max(1000, int(period * (os.cpu_count() or 1) * percent / 100))
        try:
            if self.v2: (self.path / "cpu.max").write_text(f"{quota} {period}"); ok = (self.path / "cpu.max").read_text().split()[0] == str(quota)
            else: (self.path / "cpu.cfs_quota_us").write_text(str(quota)); ok = int((self.path / "cpu.cfs_quota_us").read_text()) == quota
        except OSError: return False
        if ok: self.percent = percent
        return ok

    def clear(self) -> bool:
        if not self.ok: return False
        try:
            if self.v2: (self.path / "cpu.max").write_text("max"); ok = (self.path / "cpu.max").read_text().split()[0] == "max"
            else: (self.path / "cpu.cfs_quota_us").write_text("-1"); ok = int((self.path / "cpu.cfs_quota_us").read_text()) == -1
        except OSError: return False
        if ok: self.percent = None
        return ok

    def close(self) -> None:
        if not self.ok: return
        for pid in (self.path / "cgroup.procs").read_text().split():
            try: (self.base / "cgroup.procs").write_text(pid)
            except OSError: pass
        try: self.path.rmdir()
        except OSError: pass
        self.ok = False
