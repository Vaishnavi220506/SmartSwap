"""One controlled trial: background interference x mitigation policy -> measured
foreground tail latency AND background throughput (the cost of protection).

Timeline of a trial
    [canary + controller start]  (only for canary-based policies; SmartSwap is
                                  assumed to be always running)
    [background workers start] -> all print READY -> t_bg_ready
    [static policies applied]
    [foreground probe] warm-up 1 s, then measures SS_DURATION seconds
    [revert every control, verify the revert] -> collect worker output -> cleanup

Every action and every revert is written to the ledger with a kernel read-back
`verified` flag. Background work is counted only inside the foreground's
measurement window so throughput is comparable across policies.
"""
from __future__ import annotations

import json
import math
import os
import statistics
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import psutil

from . import native
from .controller import Calibration, Controller, ControllerConfig
from .probes import LOOPER, PROBE, UIPROBE, WORKER, ensure_data_files

CPU_COUNT = os.cpu_count() or 4
DATA_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "SmartSwap" / "data"  # outside OneDrive: 1 GiB dataset

PRESETS: dict[str, list[tuple[str, int]]] = {
    "idle": [],
    "cpu": [("cpu", 2 * CPU_COUNT)],
    "membw": [("membw", max(2, CPU_COUNT // 2))],
    "io": [("io", 16)],
    "mixed": [("cpu", CPU_COUNT), ("membw", max(1, CPU_COUNT // 4)), ("io", 4)],
}
PRESET_LABELS = {"idle": "No interference (control)", "cpu": "CPU contention", "membw": "Memory-bandwidth contention", "io": "Storage-read contention", "mixed": "Mixed interference"}

# Real tools as background work (research harness only; the dashboard keeps the
# synthetic presets). Each entry is (tool, copies); tools are built per host by
# `real_command`, using only what ships with Windows and Python.
REAL_PRESETS: dict[str, list[tuple[str, int]]] = {
    "real_build": [("compile", max(2, CPU_COUNT // 2))],
    "real_hash": [("hash", 4)],
    "real_archive": [("archive", 2)],
    "real_search": [("search", 4)],
    "real_mixed": [("compile", max(1, CPU_COUNT // 4)), ("hash", 2), ("search", 2)],
}
PRESET_LABELS.update({"real_build": "Real: byte-compiling the Python stdlib (build-like)", "real_hash": "Real: certutil SHA-256 of a 1 GiB file (sync/backup-like)",
                      "real_archive": "Real: Compress-Archive of the stdlib (backup-like)", "real_search": "Real: findstr over the stdlib (indexer-like)",
                      "real_mixed": "Real: build + hash + search"})
ALL_PRESETS = {**PRESETS, **REAL_PRESETS}


def real_command(tool: str, bg_file: Path, scratch: Path, copy: int = 0) -> list[str]:
    stdlib = str(Path(os.__file__).parent)
    if tool == "compile": return [sys.executable, "-m", "compileall", "-f", "-q", "-j", "1", stdlib]  # .pyc go to PYTHONPYCACHEPREFIX, never into the install
    if tool == "hash": return ["certutil", "-hashfile", str(bg_file), "SHA256"]
    if tool == "archive": return ["powershell", "-NoProfile", "-Command", f"Compress-Archive -Path '{stdlib}\\*.py' -DestinationPath '{scratch / f'archive-{os.getpid()}-{copy}.zip'}' -Force"]
    if tool == "search": return ["findstr", "/s", "/i", "/m", "SmartSwapNeedleThatIsNeverFound", stdlib + "\\*.py"]
    raise ValueError(f"unknown tool {tool}")

POLICIES = {
    "none": "No mitigation",
    "observe": "SmartSwap canary + controller, actions disabled (overhead/attribution)",
    "nice": "Static deprioritisation: IDLE priority class + very-low I/O priority on all background work",
    "static": "SmartSwap v1: unconditional 35% Job CPU-rate cap + ABOVE_NORMAL foreground",
    "adaptive": "SmartSwap v2: WSI-gated, attribution-routed ladder with AIMD CPU-rate control",
    "adaptive_noaimd": "Ablation: v2 ladder with a fixed 50% cap (no AIMD)",
    "adaptive_noladder": "Ablation: v2 AIMD cap without the resource-matched hint stage",
    "adaptive_nobenefit": "Ablation: v2 without benefit verification (ineffective caps are kept)",
    "adaptive_slo1": "SLO sweep: v2 with a strict 1.5% WSI target",
    "adaptive_slo6": "SLO sweep: v2 with a lenient 6% WSI target (engages at 8%)",
    "ecore": "Static core-class steering: confine all background work to E-cores (mechanism test)",
    "adaptive_hybrid": "SmartSwap v2 + core-class steering: hint -> E-core steering -> AIMD cap",
    "ecoqos": "Windows 11 Efficiency mode (what Task Manager applies): EcoQoS throttling + IDLE priority on all background work",
    "adaptive_eco": "SmartSwap v2 + EcoQoS rung: hint -> [E-core steering] -> EcoQoS -> AIMD cap",
}
CANARY_POLICIES = {"observe", "adaptive", "adaptive_noaimd", "adaptive_noladder", "adaptive_nobenefit", "adaptive_slo1", "adaptive_slo6", "adaptive_hybrid", "adaptive_eco"}
STATIC_CAP_PERCENT = 35.0

FG_ENV = {"SS_INTERVAL": "0.05", "SS_WARMUP": "1.0", "SS_DURATION": "6.0", "SS_COMPUTE_ITERS": "12000", "SS_MEM_BYTES": str(8 << 20)}
CANARY_ENV = {"SS_INTERVAL": "0.04", "SS_WARMUP": "0.4", "SS_DURATION": "0", "SS_COMPUTE_ITERS": "3000", "SS_MEM_BYTES": str(8 << 20), "SS_STREAM": "1"}
CONTROL_PERIOD_S = 0.2
GROUP_ACTIVE_CORES = 0.1   # governed group counts as busy above a tenth of a core
GROUP_ACTIVE_MBPS = 1.0    # ... or above 1 MB/s of I/O
DEADLINE_MS = 25.0  # a beat whose response exceeds half the 50 ms period is a miss

_LIVE: set[subprocess.Popen] = set()
_LIVE_LOCK = threading.Lock()


def controller_config(policy: str) -> ControllerConfig:
    cfg = ControllerConfig()
    if policy == "observe": cfg.act = False
    if policy == "adaptive_noaimd": cfg.enable_aimd = False
    if policy == "adaptive_noladder": cfg.enable_ladder = False
    if policy == "adaptive_nobenefit": cfg.enable_benefit_check = False
    if policy == "adaptive_slo1": cfg.slo_wsi = 1.5
    if policy == "adaptive_slo6": cfg.slo_wsi = 6.0; cfg.engage_wsi = 8.0
    if policy == "adaptive_hybrid": cfg.enable_steer = native.efficient_core_mask() != 0
    if policy == "adaptive_eco": cfg.enable_eco = True; cfg.enable_steer = native.efficient_core_mask() != 0
    return cfg


def _spawn(code: str, env: dict[str, str], stdout: bool = True) -> subprocess.Popen:
    proc = subprocess.Popen([sys.executable, "-c", code], env=env, stdout=subprocess.PIPE if stdout else subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    with _LIVE_LOCK: _LIVE.add(proc)
    return proc


def _reap(proc: subprocess.Popen | None) -> None:
    if proc is None: return
    try:
        if proc.poll() is None: proc.kill()
        proc.wait(timeout=5)
    except Exception: pass
    with _LIVE_LOCK: _LIVE.discard(proc)


def kill_all() -> int:
    """Emergency cleanup: terminate every SmartSwap-owned child still alive."""
    with _LIVE_LOCK: live = list(_LIVE)
    for proc in live: _reap(proc)
    return len(live)


def _env(extra: dict[str, str]) -> dict[str, str]:
    env = os.environ.copy(); env.update(extra); return env


def beat_latency(row: dict[str, float]) -> float:
    """Response time of one foreground beat: the probe's own components, or a real app's UI round trip."""
    return row["ui"] if "ui" in row else row["sched"] + row["cpu"] + row["mem"] + row["io"]


def summarize_rows(rows: list[dict[str, float]], interval_ms: float, duration_ms: float) -> dict[str, Any]:
    if not rows: return {"beats": 0}
    lat = sorted(beat_latency(r) for r in rows)
    q = lambda p: lat[min(len(lat) - 1, max(0, math.ceil(p * len(lat) - 1e-9) - 1))]  # nearest-rank percentile
    expected = int(duration_ms // interval_ms)
    return {
        "beats": len(rows), "expected_beats": expected, "skipped_beats": max(0, expected - len(rows)),
        "p50_ms": round(q(.50), 3), "p90_ms": round(q(.90), 3), "p95_ms": round(q(.95), 3), "p99_ms": round(q(.99), 3), "max_ms": round(lat[-1], 3),
        "mean_ms": round(statistics.fmean(lat), 3), "deadline_misses": sum(1 for x in lat if x > DEADLINE_MS),
        "component_median_ms": {k: round(statistics.median(r[k] for r in rows), 4) for k in ("sched", "cpu", "mem", "io")},
        "component_p95_ms": {k: round(sorted(r[k] for r in rows)[min(len(rows) - 1, math.ceil(0.95 * len(rows) - 1e-9) - 1)], 4) for k in ("sched", "cpu", "mem", "io")},
    }


def calibrate_canary(seconds: float = 4.0) -> Calibration:
    """Idle canary medians; must be measured with no SmartSwap background load."""
    _, probe_file = ensure_data_files(DATA_DIR)
    env = _env({**CANARY_ENV, "SS_STREAM": "0", "SS_DURATION": str(seconds), "SS_IO_FILE": str(probe_file)})
    proc = _spawn(PROBE, env)
    try: out, _ = proc.communicate(timeout=seconds + 20)
    finally: _reap(proc)
    return Calibration.from_rows(json.loads(out.strip().splitlines()[-1])["rows"])


@dataclass
class Ledger:
    t0: float
    entries: list[dict[str, Any]] = field(default_factory=list)

    def add(self, kind: str, target: str, value: Any, verified: bool, reason: str = "") -> bool:
        self.entries.append({"t_s": round(time.time() - self.t0, 3), "kind": kind, "target": target, "value": value, "verified": bool(verified), "reason": reason})
        return verified


class Actuator:
    """Applies controller decisions to the SmartSwap-owned background workers.

    With `track_children`, descendants the workers start later (real tools run
    by a looper) are discovered by `refresh()` and receive the active hints.
    """

    def __init__(self, pids: list[int], ledger: Ledger, name: str, track_children: bool = False) -> None:
        self.roots = list(pids); self.pids: list[int] = []; self.ledger = ledger; self.name = name; self.job: native.Job | None = None
        self.track_children = track_children; self.lock = threading.RLock(); self.active: set[str] = set()
        self.procs: list[psutil.Process] = []; self.original_priority: dict[int, int | None] = {}; self.original_io: dict[int, int | None] = {}
        self.original_affinity: dict[int, int] = {}; self.original_eco: dict[int, tuple[int, int] | None] = {}
        self._adopt(pids)

    def _adopt(self, pids: list[int]) -> None:
        for pid in pids:
            if pid in self.original_priority: continue
            try: self.procs.append(psutil.Process(pid))
            except psutil.Error: continue
            self.pids.append(pid)
            self.original_priority[pid] = native.get_priority_class(pid); self.original_io[pid] = native.get_io_priority(pid)
            self.original_affinity[pid] = (native.get_affinity(pid) or (0, 0))[0]; self.original_eco[pid] = native.get_power_throttling(pid)

    def refresh(self) -> None:
        """Adopt new descendants of the workers and give them the hints already in force."""
        if not self.track_children: return
        with self.lock:
            fresh = []
            for root in self.roots:
                try: fresh += [c.pid for c in psutil.Process(root).children(recursive=True) if c.pid not in self.original_priority]
                except psutil.Error: continue
            if not fresh: return
            self._adopt(fresh)
            if self.job is not None:
                for pid in fresh: self.job.assign(pid)
            for hint in sorted(self.active):
                {"priority": self.cpu_hint, "io_priority": self.io_hint, "steer": self.steer, "eco": self.eco_hint}[hint]("new child process", pids=fresh)

    def group_usage(self) -> tuple[float, float]:
        """Cumulative CPU seconds and bytes read+written by the governed workers."""
        cpu = io = 0.0
        for proc in self.procs:
            try:
                t = proc.cpu_times(); c = proc.io_counters()
                cpu += t.user + t.system; io += c.read_bytes + c.write_bytes
            except (psutil.Error, OSError): continue
        return cpu, io

    def _ensure_job(self) -> bool:
        if self.job is None:
            self.job = native.Job(self.name)
            members = sum(self.job.assign(pid) for pid in self.pids)
            self.ledger.add("job_assign", f"{len(self.pids)} workers", members, members == len(self.pids) and self.job.ok)
        return bool(self.job and self.job.ok)

    def _alive(self, pids: list[int]) -> list[int]:
        return [pid for pid in pids if psutil.pid_exists(pid)]

    def cpu_hint(self, reason: str = "", pids: list[int] | None = None) -> None:
        targets = self._alive(pids or self.pids); self.active.add("priority")
        ok = all([native.set_priority_class(pid, native.IDLE_PRIORITY_CLASS) for pid in targets])
        self.ledger.add("priority_idle", f"{len(targets)} workers", "IDLE_PRIORITY_CLASS", ok, reason)

    def io_hint(self, reason: str = "", pids: list[int] | None = None) -> None:
        targets = self._alive(pids or self.pids); self.active.add("io_priority")
        ok = all([native.set_io_priority(pid, native.IO_PRIORITY_VERY_LOW) for pid in targets])
        self.ledger.add("io_priority_very_low", f"{len(targets)} workers", 0, ok, reason)

    def steer(self, reason: str = "", pids: list[int] | None = None) -> None:
        targets = self._alive(pids or self.pids); self.active.add("steer"); mask = native.efficient_core_mask()
        ok = bool(mask) and all([native.set_affinity(pid, mask) for pid in targets])
        self.ledger.add("steer_ecores", f"{len(targets)} workers", hex(mask), ok, reason)

    def eco_hint(self, reason: str = "", pids: list[int] | None = None) -> None:
        targets = self._alive(pids or self.pids); self.active.add("eco")
        ok = all([native.set_power_throttling(pid, True) for pid in targets])
        self.ledger.add("ecoqos_on", f"{len(targets)} workers", "EXECUTION_SPEED", ok, reason)

    def set_cap(self, percent: float, reason: str = "") -> None:
        ok = self._ensure_job() and self.job.set_cpu_rate(percent)
        self.ledger.add("cpu_rate_cap", "job", percent, ok, reason)

    def clear_cap(self, reason: str = "") -> None:
        if self.job is not None and self.job.cpu_rate_percent is not None:
            self.ledger.add("cpu_rate_cap_revert", "job", None, self.job.clear_cpu_rate(), reason)

    def revert_hint(self, hint: str, reason: str = "") -> None:
        self.active.discard(hint)
        alive = [pid for pid in self.pids if native.get_priority_class(pid) is not None]
        if hint == "priority":
            ok = all([native.set_priority_class(pid, self.original_priority.get(pid) or native.NORMAL_PRIORITY_CLASS) for pid in alive])
            self.ledger.add("priority_revert", f"{len(alive)} workers", "original", ok, reason)
        elif hint == "steer":
            ok = all([native.set_affinity(pid, self.original_affinity.get(pid) or 0xFFFFFFFFFFFFFFFF & ((1 << CPU_COUNT) - 1)) for pid in alive])
            self.ledger.add("steer_revert", f"{len(alive)} workers", "original affinity", ok, reason)
        elif hint == "io_priority":
            ok = all([native.set_io_priority(pid, self.original_io.get(pid) if self.original_io.get(pid) is not None else native.IO_PRIORITY_NORMAL) for pid in alive])
            self.ledger.add("io_priority_revert", f"{len(alive)} workers", "original", ok, reason)
        elif hint == "eco":
            ok = all([native.set_power_throttling(pid, None if not self.original_eco.get(pid) or not self.original_eco[pid][0] else bool(self.original_eco[pid][1])) for pid in alive])
            self.ledger.add("ecoqos_revert", f"{len(alive)} workers", "original", ok, reason)

    def apply(self, decision: Any) -> None:
        with self.lock:
            if decision.kind == "hint": {"priority": self.cpu_hint, "io_priority": self.io_hint, "steer": self.steer, "eco": self.eco_hint}[decision.resource](decision.reason)
            elif decision.kind == "cap_set": self.set_cap(decision.value, decision.reason)
            elif decision.kind == "cap_clear": self.clear_cap(decision.reason)
            elif decision.kind == "hint_revert": self.revert_hint(decision.resource, decision.reason)

    def revert_all(self, hints: set[str]) -> None:
        with self.lock:
            self.clear_cap("end of trial")
            for hint in sorted(hints | self.active): self.revert_hint(hint, "end of trial")
            if self.job: self.job.close()


def _window_units(buckets: dict[str, int], start: float, end: float) -> int:
    lo, hi = int(start * 10), int(end * 10)
    return sum(n for b, n in buckets.items() if lo <= int(b) < hi)


def _launch_foreground(foreground: str, seed: int) -> tuple[subprocess.Popen, list[psutil.Process]]:
    """Start the foreground measurement: the synthetic probe, or a real app's UI round trip ("ui:<exe>").

    For a real app, the exe is launched if needed and every process of that name
    started by us is returned so it can be closed after the trial.
    """
    _, probe_file = ensure_data_files(DATA_DIR)
    if foreground == "probe": return _spawn(PROBE, _env({**FG_ENV, "SS_IO_FILE": str(probe_file), "SS_SEED": str(seed + 7)})), []
    if not foreground.startswith("ui:"): raise ValueError(f"unknown foreground {foreground}")
    exe = foreground[3:]; launched_at = time.time() - 1.0
    subprocess.Popen([exe], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    probe = _spawn(UIPROBE, _env({**FG_ENV, "SS_UI_EXE": exe, "SS_UI_NOT_BEFORE": str(launched_at)}))
    ours = []
    for proc in psutil.process_iter(["name", "create_time"]):
        if (proc.info["name"] or "").lower() == exe.lower() and (proc.info["create_time"] or 0) >= launched_at: ours.append(proc)
    return probe, ours


def _close_launched(procs: list[psutil.Process], exe: str, launched_after: float) -> None:
    for proc in psutil.process_iter(["name", "create_time"]):  # an app alias may have re-launched under a new pid
        if (proc.info["name"] or "").lower() == exe.lower() and (proc.info["create_time"] or 0) >= launched_after and proc not in procs: procs.append(proc)
    for proc in procs:
        try: proc.kill()
        except psutil.Error: pass


def run_trial(preset: str, policy: str, calibration: Calibration | None = None, cancel: threading.Event | None = None, seed: int = 0, foreground: str = "probe") -> dict[str, Any]:
    """One trial. `foreground` is "probe" (synthetic beats) or "ui:<exe>" (a real app's UI latency)."""
    if preset not in ALL_PRESETS: raise ValueError(f"unknown preset {preset}")
    if policy not in POLICIES: raise ValueError(f"unknown policy {policy}")
    if policy in CANARY_POLICIES and calibration is None: raise ValueError("canary policies need an idle calibration")
    bg_file, probe_file = ensure_data_files(DATA_DIR)
    t0 = time.time(); ledger = Ledger(t0); errors: list[str] = []
    workers: list[tuple[str, subprocess.Popen]] = []; canary = fg = None
    controller: Controller | None = None; actuator: Actuator | None = None
    stop = threading.Event(); canary_rows: list[dict[str, float]] = []; ctl_thread = reader = None
    fg_out: dict[str, Any] = {}; worker_out: list[dict[str, Any]] = []; t_bg_ready = None; percpu: list[float] = []
    real = preset in REAL_PRESETS; launched_apps: list[psutil.Process] = []; canary_cpu: tuple[float, float] | None = None
    stop_event = native.StopEvent(rf"Local\SmartSwapStop-{os.getpid()}-{int(t0 * 1e6)}")
    try:
        if policy in CANARY_POLICIES:
            canary = _spawn(PROBE, _env({**CANARY_ENV, "SS_IO_FILE": str(probe_file), "SS_SEED": str(seed + 101)}))
            if canary.stdout.readline().strip() != "READY": raise RuntimeError("canary failed to start: " + canary.stderr.read()[-400:])

            def read_canary() -> None:
                for line in canary.stdout:
                    if line.startswith("{"): canary_rows.append(json.loads(line))
            reader = threading.Thread(target=read_canary, daemon=True); reader.start()
            controller = Controller(calibration, float(CANARY_ENV["SS_INTERVAL"]) * 1000, controller_config(policy))

        for role, count in ALL_PRESETS[preset]:
            for i in range(count):
                if real:
                    env = _env({"SS_ROLE": role, "SS_STOP_EVENT": stop_event.name, "SS_CMD": json.dumps(real_command(role, bg_file, DATA_DIR, len(workers))), "PYTHONPYCACHEPREFIX": str(DATA_DIR / "pycache")})
                    workers.append((role, _spawn(LOOPER, env))); continue
                env = _env({"SS_ROLE": role, "SS_STOP_EVENT": stop_event.name, "SS_IO_FILE": str(bg_file), "SS_BG_IO_BLOCK": str(1 << 20), "SS_BG_MEM_BYTES": str(64 << 20), "SS_SEED": str(seed * 1000 + len(workers))})
                workers.append((role, _spawn(WORKER, env)))
        for role, proc in workers:
            if proc.stdout.readline().strip() != "READY": raise RuntimeError(f"{role} worker failed: " + proc.stderr.read()[-400:])
        t_bg_ready = time.time()
        pids = [proc.pid for _, proc in workers]
        if pids: actuator = Actuator(pids, ledger, f"SmartSwap-{os.getpid()}-{int(t0 * 1000)}", track_children=real)
        if actuator and real:  # real tools keep starting new processes: hand them the hints in force
            def refresh_loop() -> None:
                while not stop.wait(CONTROL_PERIOD_S): actuator.refresh()
            threading.Thread(target=refresh_loop, daemon=True).start()

        if controller is not None:
            def control_loop() -> None:
                last = actuator.group_usage() if actuator else None; last_t = time.time()
                while not stop.wait(CONTROL_PERIOD_S):
                    active = None
                    if actuator:
                        now_usage, now_t = actuator.group_usage(), time.time()
                        cores = (now_usage[0] - last[0]) / max(1e-3, now_t - last_t); mbps = (now_usage[1] - last[1]) / 1e6 / max(1e-3, now_t - last_t)
                        active = cores >= GROUP_ACTIVE_CORES or mbps >= GROUP_ACTIVE_MBPS; last, last_t = now_usage, now_t
                    for decision in controller.step((time.time() - t0) * 1000, canary_rows[-controller.cfg.window_beats:], active):
                        if actuator: actuator.apply(decision)
            ctl_thread = threading.Thread(target=control_loop, daemon=True); ctl_thread.start()

        fg, launched_apps = _launch_foreground(foreground, seed)
        if policy == "nice" and actuator:
            actuator.cpu_hint("static policy"); actuator.io_hint("static policy")
        if policy == "ecore" and actuator: actuator.steer("static policy")
        if policy == "ecoqos" and actuator:
            actuator.eco_hint("static policy"); actuator.cpu_hint("static policy")
        psutil.cpu_percent(percpu=True)  # start the per-core utilisation window
        if policy == "static":
            if actuator: actuator.set_cap(STATIC_CAP_PERCENT, "static policy")
            ledger.add("foreground_above_normal", "foreground", "ABOVE_NORMAL_PRIORITY_CLASS", native.set_priority_class(fg.pid, native.ABOVE_NORMAL_PRIORITY_CLASS), "static policy")

        deadline = time.time() + 60
        while True:  # communicate() drains the pipe; polling alone deadlocks on a full pipe buffer
            try: out, err = fg.communicate(timeout=0.25); break
            except subprocess.TimeoutExpired:
                if cancel is not None and cancel.is_set(): raise RuntimeError("cancelled")
                if time.time() > deadline: raise RuntimeError("foreground probe timed out")
        if fg.returncode != 0: raise RuntimeError("foreground probe failed: " + err[-400:])
        fg_out = json.loads(out.strip().splitlines()[-1])
        percpu = psutil.cpu_percent(percpu=True)  # mean busy % per logical CPU over the foreground run
    except Exception as exc:  # recorded, never swallowed silently
        errors.append(f"{type(exc).__name__}: {exc}")
    finally:
        stop.set()
        if canary is not None:  # overhead: the canary's own CPU use over its lifetime
            try:
                ct = psutil.Process(canary.pid).cpu_times(); canary_cpu = (ct.user + ct.system, time.time() - psutil.Process(canary.pid).create_time())
            except psutil.Error: pass
        if ctl_thread: ctl_thread.join(timeout=2)
        if actuator:  # revert while workers are still alive so the read-back is meaningful
            applied = controller.hints if controller and controller.cfg.act else ({"priority", "io_priority"} if policy == "nice" else {"steer"} if policy == "ecore" else {"eco", "priority"} if policy == "ecoqos" else set())
            actuator.revert_all(applied)
        stop_event.set()
        for role, proc in workers:
            try:
                out, _ = proc.communicate(timeout=15)
                worker_out.append({"role": role, **json.loads(out.strip().splitlines()[-1])})
            except Exception as exc:
                errors.append(f"worker {role}: {type(exc).__name__}: {exc}")
            _reap(proc)
        _reap(canary); _reap(fg); stop_event.close()
        if launched_apps or foreground.startswith("ui:"): _close_launched(launched_apps, foreground[3:], t0 - 1.0)

    rows = fg_out.get("rows", [])
    summary = summarize_rows(rows, float(FG_ENV["SS_INTERVAL"]) * 1000, fg_out.get("duration_ms", 0.0))
    win = (fg_out.get("wall_start"), fg_out.get("wall_end"))
    throughput: dict[str, float] = {}
    if win[0] and win[1]:
        for w in worker_out:
            throughput[w["role"]] = throughput.get(w["role"], 0) + _window_units(w.get("buckets", {}), win[0], win[1]) / (win[1] - win[0])
    actions = [e for e in ledger.entries if not e["kind"].endswith("_revert")]
    reverts = [e for e in ledger.entries if e["kind"].endswith("_revert")]
    first_action = next((e["t_s"] for e in actions), None)
    trace = controller.trace if controller else []
    return {
        "preset": preset, "policy": policy, "seed": seed, "started_at": t0, "errors": errors,
        "foreground": summary, "foreground_rows": rows,
        "background_units_per_s": {k: round(v, 3) for k, v in throughput.items()},
        "background_workers": len(workers),
        "actions": ledger.entries, "action_count": len(actions), "actions_verified": sum(e["verified"] for e in actions),
        "reverts_verified": all(e["verified"] for e in reverts) if reverts else None,
        "time_to_first_action_s": round(first_action - (t_bg_ready - t0), 3) if first_action is not None and t_bg_ready else None,
        "controller_trace": trace,
        "controller_engaged_share": round(sum(1 for s in trace if s["engaged"]) / len(trace), 3) if trace else None,
        "final_cap": controller.cap if controller else (STATIC_CAP_PERCENT if policy == "static" and workers else None),
        "canary_beats": len(canary_rows), "calibration": asdict(calibration) if calibration else None,
        "percpu_busy": percpu, "core_classes": {str(k): v for k, v in native.core_classes().items()},
        "foreground_mode": foreground, "ui_timeouts": sum(1 for r in rows if r.get("timeout")),
        "canary_cpu_share": round(canary_cpu[0] / canary_cpu[1], 5) if canary_cpu and canary_cpu[1] > 0 else None,  # fraction of one core
        "background_io_mb": round(sum(v for w in worker_out for b, v in (w.get("io_mb_buckets") or {}).items() if win[0] and win[1] and int(win[0] * 10) <= int(b) < int(win[1] * 10)), 3) if real else None,
    }


class LiveMonitor:
    """Continuous canary for the dashboard.

    It measures the Windows Stall Index on the real desktop, reports what the
    controller *would* do, and ranks the applications most likely causing the
    stall (blame.py). By default it never acts. With `govern=True` (opt-in,
    optionally limited to an allowlist of executable names) it confirms
    suspects by intervention and lowers the confirmed culprits' priority
    through the journaled, lowering-only ExternalGovernor (govern.py).
    """

    HISTORY = 300  # control periods kept (60 s at 200 ms)

    def __init__(self) -> None:
        self.lock = threading.Lock(); self.proc: subprocess.Popen | None = None; self.stop_flag = threading.Event()
        self.rows: list[dict[str, float]] = []; self.trace: list[dict[str, Any]] = []
        self.controller: Controller | None = None; self.started_at: float | None = None; self.error: str | None = None
        self.blamer: Any = None; self.interrogator: Any = None; self.governor: Any = None; self.ledger: Ledger | None = None; self.suspects: list[dict[str, Any]] = []

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, calibration: Calibration, govern: bool = False, allow: set[str] | None = None) -> None:
        from . import blame, govern as gov  # govern imports this module
        with self.lock:
            if self.running: return
            _, probe_file = ensure_data_files(DATA_DIR)
            self.rows, self.trace, self.error, self.suspects = [], [], None, []
            self.controller = Controller(calibration, float(CANARY_ENV["SS_INTERVAL"]) * 1000, ControllerConfig(act=False))
            self.proc = _spawn(PROBE, _env({**CANARY_ENV, "SS_IO_FILE": str(probe_file)}))
            self.blamer = blame.Blamer(CONTROL_PERIOD_S, engage_wsi=self.controller.cfg.engage_wsi, exclude_pids=frozenset({os.getpid(), self.proc.pid}))
            self.ledger = Ledger(time.time()); self.interrogator = self.governor = None
            if govern:
                gov.recover()
                self.interrogator = blame.make_interrogator("factorial", slo_wsi=self.controller.cfg.slo_wsi)
                self.governor = gov.ExternalGovernor(self.ledger, allow=allow)
            self.stop_flag.clear(); self.started_at = time.time()
            threading.Thread(target=self._read, daemon=True).start()
            threading.Thread(target=self._control, daemon=True).start()

    def _read(self) -> None:
        proc = self.proc
        try:
            for line in proc.stdout:
                if line.startswith("{"):
                    row = json.loads(line)
                    with self.lock:
                        self.rows.append(row); del self.rows[:-64]
        except Exception as exc: self.error = f"{type(exc).__name__}: {exc}"

    def _control(self) -> None:
        while not self.stop_flag.wait(CONTROL_PERIOD_S):
            try: self._blame()  # outside the lock: a process snapshot can take tens of ms
            except Exception as exc: self.error = f"blame: {type(exc).__name__}: {exc}"
            with self.lock:
                if not self.controller or not self.rows: continue
                window = self.rows[-self.controller.cfg.window_beats:]
                self.controller.step((time.time() - self.started_at) * 1000, window, None)
                row = dict(self.controller.trace[-1]); row["t"] = time.time()
                row["median_ms"] = {k: round(statistics.median(r[k] for r in window), 4) for k in ("sched", "cpu", "mem", "io")}
                self.trace.append(row); del self.trace[:-self.HISTORY]

    def _blame(self) -> None:
        """Rank suspects against the previous control period's WSI (and act, if governing)."""
        from .govern import snapshot
        ctl = self.controller
        if ctl is None or not ctl.trace or self.blamer is None: return
        row = ctl.trace[-1]
        self.blamer.update(snapshot(), row["wsi"], row["excess"], native.foreground_pid())
        resource = row["attributed"] if row["attributed"] != "none" else ctl.resource
        ranked = self.blamer.rank(resource) if resource in ("cpu", "mem", "io") else []
        self.suspects = [s.as_dict() for s in ranked[:5]]
        if self.interrogator is not None:
            for d in self.interrogator.step(ctl.engaged, ctl.resource, ctl.wsi_history, ranked, self.blamer.names):
                self.governor.apply(d, self.blamer.members.get(d.app, [d.app]))

    def stop(self) -> None:
        with self.lock:
            self.stop_flag.set(); proc, self.proc = self.proc, None
            if self.governor is not None: self.governor.release_all("live monitor stopped")
        _reap(proc)

    def state(self) -> dict[str, Any]:
        with self.lock:
            ctl = self.controller
            return {"running": self.running, "started_at": self.started_at, "error": self.error, "calibration": ctl.cal.as_dict() if ctl else None,
                    "thresholds": {"engage_wsi": ctl.cfg.engage_wsi, "slo_wsi": ctl.cfg.slo_wsi, "release_wsi": ctl.cfg.release_wsi, "attribution_floor": ctl.cfg.attribution_floor} if ctl else None,
                    "would_level": ctl.level if ctl else 0, "would_cap": ctl.cap if ctl else None, "trace": list(self.trace),
                    "suspects": list(self.suspects), "governing": self.governor is not None,
                    "verdicts": list(self.interrogator.verdicts[-20:]) if self.interrogator else [], "ledger": list(self.ledger.entries[-50:]) if self.ledger else []}


LIVE_MONITOR = LiveMonitor()
