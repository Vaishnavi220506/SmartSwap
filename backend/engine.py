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
from .probes import PROBE, WORKER, ensure_data_files

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
}
CANARY_POLICIES = {"observe", "adaptive", "adaptive_noaimd", "adaptive_noladder", "adaptive_nobenefit", "adaptive_slo1", "adaptive_slo6", "adaptive_hybrid"}
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


def summarize_rows(rows: list[dict[str, float]], interval_ms: float, duration_ms: float) -> dict[str, Any]:
    if not rows: return {"beats": 0}
    lat = sorted(r["sched"] + r["cpu"] + r["mem"] + r["io"] for r in rows)
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
    """Applies controller decisions to the SmartSwap-owned background workers."""

    def __init__(self, pids: list[int], ledger: Ledger, name: str) -> None:
        self.pids = pids; self.ledger = ledger; self.name = name; self.job: native.Job | None = None
        self.procs = [psutil.Process(pid) for pid in pids]
        self.original_priority = {pid: native.get_priority_class(pid) for pid in pids}
        self.original_io = {pid: native.get_io_priority(pid) for pid in pids}
        self.original_affinity = {pid: (native.get_affinity(pid) or (0, 0))[0] for pid in pids}

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

    def cpu_hint(self, reason: str = "") -> None:
        ok = all([native.set_priority_class(pid, native.IDLE_PRIORITY_CLASS) for pid in self.pids])
        self.ledger.add("priority_idle", f"{len(self.pids)} workers", "IDLE_PRIORITY_CLASS", ok, reason)

    def io_hint(self, reason: str = "") -> None:
        ok = all([native.set_io_priority(pid, native.IO_PRIORITY_VERY_LOW) for pid in self.pids])
        self.ledger.add("io_priority_very_low", f"{len(self.pids)} workers", 0, ok, reason)

    def steer(self, reason: str = "") -> None:
        mask = native.efficient_core_mask()
        ok = bool(mask) and all([native.set_affinity(pid, mask) for pid in self.pids])
        self.ledger.add("steer_ecores", f"{len(self.pids)} workers", hex(mask), ok, reason)

    def set_cap(self, percent: float, reason: str = "") -> None:
        ok = self._ensure_job() and self.job.set_cpu_rate(percent)
        self.ledger.add("cpu_rate_cap", "job", percent, ok, reason)

    def clear_cap(self, reason: str = "") -> None:
        if self.job is not None and self.job.cpu_rate_percent is not None:
            self.ledger.add("cpu_rate_cap_revert", "job", None, self.job.clear_cpu_rate(), reason)

    def revert_hint(self, hint: str, reason: str = "") -> None:
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

    def apply(self, decision: Any) -> None:
        if decision.kind == "hint": {"priority": self.cpu_hint, "io_priority": self.io_hint, "steer": self.steer}[decision.resource](decision.reason)
        elif decision.kind == "cap_set": self.set_cap(decision.value, decision.reason)
        elif decision.kind == "cap_clear": self.clear_cap(decision.reason)
        elif decision.kind == "hint_revert": self.revert_hint(decision.resource, decision.reason)

    def revert_all(self, hints: set[str]) -> None:
        self.clear_cap("end of trial")
        for hint in sorted(hints): self.revert_hint(hint, "end of trial")
        if self.job: self.job.close()


def _window_units(buckets: dict[str, int], start: float, end: float) -> int:
    lo, hi = int(start * 10), int(end * 10)
    return sum(n for b, n in buckets.items() if lo <= int(b) < hi)


def run_trial(preset: str, policy: str, calibration: Calibration | None = None, cancel: threading.Event | None = None, seed: int = 0) -> dict[str, Any]:
    if preset not in PRESETS: raise ValueError(f"unknown preset {preset}")
    if policy not in POLICIES: raise ValueError(f"unknown policy {policy}")
    if policy in CANARY_POLICIES and calibration is None: raise ValueError("canary policies need an idle calibration")
    bg_file, probe_file = ensure_data_files(DATA_DIR)
    t0 = time.time(); ledger = Ledger(t0); errors: list[str] = []
    workers: list[tuple[str, subprocess.Popen]] = []; canary = fg = None
    controller: Controller | None = None; actuator: Actuator | None = None
    stop = threading.Event(); canary_rows: list[dict[str, float]] = []; ctl_thread = reader = None
    fg_out: dict[str, Any] = {}; worker_out: list[dict[str, Any]] = []; t_bg_ready = None; percpu: list[float] = []
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

        for role, count in PRESETS[preset]:
            for i in range(count):
                env = _env({"SS_ROLE": role, "SS_STOP_EVENT": stop_event.name, "SS_IO_FILE": str(bg_file), "SS_BG_IO_BLOCK": str(1 << 20), "SS_BG_MEM_BYTES": str(64 << 20), "SS_SEED": str(seed * 1000 + len(workers))})
                workers.append((role, _spawn(WORKER, env)))
        for role, proc in workers:
            if proc.stdout.readline().strip() != "READY": raise RuntimeError(f"{role} worker failed: " + proc.stderr.read()[-400:])
        t_bg_ready = time.time()
        pids = [proc.pid for _, proc in workers]
        if pids: actuator = Actuator(pids, ledger, f"SmartSwap-{os.getpid()}-{int(t0 * 1000)}")

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

        fg_env = _env({**FG_ENV, "SS_IO_FILE": str(probe_file), "SS_SEED": str(seed + 7)})
        fg = _spawn(PROBE, fg_env)
        if policy == "nice" and actuator:
            actuator.cpu_hint("static policy"); actuator.io_hint("static policy")
        if policy == "ecore" and actuator: actuator.steer("static policy")
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
        if ctl_thread: ctl_thread.join(timeout=2)
        if actuator:  # revert while workers are still alive so the read-back is meaningful
            applied = controller.hints if controller and controller.cfg.act else ({"priority", "io_priority"} if policy == "nice" else {"steer"} if policy == "ecore" else set())
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
    }


class LiveMonitor:
    """Continuous, observe-only canary for the dashboard.

    It measures the Windows Stall Index on the real desktop and reports what
    the controller *would* do. It never acts: on a live desktop SmartSwap only
    governs processes it started, and the live view governs none.
    """

    HISTORY = 300  # control periods kept (60 s at 200 ms)

    def __init__(self) -> None:
        self.lock = threading.Lock(); self.proc: subprocess.Popen | None = None; self.stop_flag = threading.Event()
        self.rows: list[dict[str, float]] = []; self.trace: list[dict[str, Any]] = []
        self.controller: Controller | None = None; self.started_at: float | None = None; self.error: str | None = None

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, calibration: Calibration) -> None:
        with self.lock:
            if self.running: return
            _, probe_file = ensure_data_files(DATA_DIR)
            self.rows, self.trace, self.error = [], [], None
            self.controller = Controller(calibration, float(CANARY_ENV["SS_INTERVAL"]) * 1000, ControllerConfig(act=False))
            self.proc = _spawn(PROBE, _env({**CANARY_ENV, "SS_IO_FILE": str(probe_file)}))
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
            with self.lock:
                if not self.controller or not self.rows: continue
                window = self.rows[-self.controller.cfg.window_beats:]
                self.controller.step((time.time() - self.started_at) * 1000, window, None)
                row = dict(self.controller.trace[-1]); row["t"] = time.time()
                row["median_ms"] = {k: round(statistics.median(r[k] for r in window), 4) for k in ("sched", "cpu", "mem", "io")}
                self.trace.append(row); del self.trace[:-self.HISTORY]

    def stop(self) -> None:
        with self.lock:
            self.stop_flag.set(); proc, self.proc = self.proc, None
        _reap(proc)

    def state(self) -> dict[str, Any]:
        with self.lock:
            ctl = self.controller
            return {"running": self.running, "started_at": self.started_at, "error": self.error, "calibration": ctl.cal.as_dict() if ctl else None,
                    "thresholds": {"engage_wsi": ctl.cfg.engage_wsi, "slo_wsi": ctl.cfg.slo_wsi, "release_wsi": ctl.cfg.release_wsi, "attribution_floor": ctl.cfg.attribution_floor} if ctl else None,
                    "would_level": ctl.level if ctl else 0, "would_cap": ctl.cap if ctl else None, "trace": list(self.trace)}


LIVE_MONITOR = LiveMonitor()
