from __future__ import annotations

import ctypes
import csv
import io
import json
import math
import os
import sqlite3
import statistics
import subprocess
import sys
import threading
import time
import uuid
from ctypes import wintypes
from pathlib import Path
from typing import Any

import psutil
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from . import engine
from .controller import Calibration
from .stats import t975, wilcoxon_signed_rank

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "smartswap.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
CALIBRATION_PATH = ROOT / "data" / "calibration" / "current_machine.json"
STUDY_DIR = ROOT / "research" / "results"
FRONTEND = ROOT / "frontend"
ACTIVE: dict[str, dict[str, Any]] = {}
ACTIVE_LOCK = threading.Lock()
TRIAL_LOCK = threading.Lock()  # trials share the machine; never let two overlap
ACTIVE_BATCHES: set[str] = set()
RESPONSE_NOISE_TOLERANCE_PCT = float(os.getenv("SMARTSWAP_RESPONSE_NOISE_TOLERANCE_PCT", "2.0"))
MIN_PAIRED_REPETITIONS = int(os.getenv("SMARTSWAP_MIN_PAIRED_REPETITIONS", "5"))
INTERFERENCE_MIN_INFLATION = 1.25  # loaded baseline p95 must exceed idle p95 by 25% to count as interference
# v1 preset ids are still accepted so old clients and exports keep working.
PRESET_ALIASES = {"cpu_contention": "cpu", "memory_pressure": "membw", "io_contention": "io", "mixed_interference": "mixed", "no_pressure_control": "idle"}
MODE_POLICY = {"baseline": "none", "optimized": "adaptive"}

app = FastAPI(title="SmartSwap for Windows", version="2.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:8765", "http://127.0.0.1:8765"], allow_methods=["*"], allow_headers=["*"])


class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD), ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong), ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong), ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong), ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, mode TEXT, started_at REAL, ended_at REAL, foreground_ms REAL, system_load REAL, action_count INTEGER, verified_count INTEGER, notes TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS samples (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, timestamp REAL, memory_load REAL, available_mb REAL, commit_used_mb REAL, commit_limit_mb REAL, foreground_rss_mb REAL, pressure_state TEXT, pressure_score REAL)")
    conn.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, timestamp REAL, kind TEXT, detail TEXT, verified INTEGER)")
    conn.execute("CREATE TABLE IF NOT EXISTS latency_samples (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, seq INTEGER, expected_ms REAL, actual_ms REAL, scheduling_delay_ms REAL, execution_ms REAL, deadline_missed INTEGER)")
    run_columns = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
    for name, definition in (("repetition", "INTEGER"), ("batch_id", "TEXT"), ("preset", "TEXT"), ("foreground_p50_ms", "REAL"), ("foreground_p95_ms", "REAL"), ("foreground_p99_ms", "REAL"), ("missed_deadlines", "INTEGER"), ("jitter_ms", "REAL"), ("longest_heartbeat_gap_ms", "REAL"), ("operations", "INTEGER"), ("measurement_duration_ms", "REAL"), ("setup_overhead_ms", "REAL"), ("validity", "TEXT"), ("bottleneck", "TEXT"), ("bottleneck_confidence", "REAL"), ("action_effectiveness", "TEXT"), ("background_throughput", "REAL"), ("action_types", "TEXT"), ("action_cooldown_seconds", "REAL"), ("recovery_observed", "INTEGER"), ("classification", "TEXT"), ("policy", "TEXT"), ("engine_version", "INTEGER"), ("error", "TEXT"), ("trace_json", "TEXT"), ("background_json", "TEXT"), ("time_to_first_action_s", "REAL")):
        if name not in run_columns: conn.execute(f"ALTER TABLE runs ADD COLUMN {name} {definition}")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(samples)")}
    for name in ("resource_score", "impact_score", "final_score", "state_transition"):
        if name not in columns: conn.execute(f"ALTER TABLE samples ADD COLUMN {name} REAL" if name != "state_transition" else "ALTER TABLE samples ADD COLUMN state_transition TEXT")
    conn.commit()
    return conn


def memory_metrics() -> dict[str, Any]:
    status = MEMORYSTATUSEX(); status.dwLength = ctypes.sizeof(status)
    ok = ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
    if not ok: raise OSError(ctypes.get_last_error(), "GlobalMemoryStatusEx failed")
    total = status.ullTotalPhys / 1048576; avail = status.ullAvailPhys / 1048576
    commit_limit = status.ullTotalPageFile / 1048576; commit_avail = status.ullAvailPageFile / 1048576
    commit_used = max(0.0, commit_limit - commit_avail)
    ps = psutil.virtual_memory()
    return {"memory_load": round(float(status.dwMemoryLoad), 2), "total_physical_mb": round(total, 1), "available_mb": round(avail, 1), "commit_used_mb": round(commit_used, 1), "commit_limit_mb": round(commit_limit, 1), "pagefile_used_mb": round(max(0.0, status.ullTotalPageFile - status.ullAvailPageFile) / 1048576, 1), "psutil_available_mb": round(ps.available / 1048576, 1), "timestamp": time.time()}


def pressure(metrics: dict[str, Any]) -> dict[str, Any]:
    memory_axis = min(100.0, max(0.0, (metrics["memory_load"] - 55) * 2.2))
    commit_axis = min(100.0, max(0.0, (metrics["commit_used_mb"] / max(metrics["commit_limit_mb"], 1) * 100 - 65) * 2.8))
    score = round(memory_axis * .6 + commit_axis * .4, 1)
    state = "critical" if score >= 80 else "elevated" if score >= 45 else "nominal"
    explanation = "Both physical-memory and commit headroom signals are healthy." if state == "nominal" else f"{state.title()} pressure: physical memory axis {memory_axis:.0f}/100; commit axis {commit_axis:.0f}/100."
    return {"state": state, "score": score, "resource_score": round(memory_axis, 1), "impact_score": round(commit_axis, 1), "memory_axis": round(memory_axis, 1), "commit_axis": round(commit_axis, 1), "explanation": explanation}


def native_capabilities() -> dict[str, Any]:
    result: dict[str, Any] = {"platform": sys.platform, "python": sys.version.split()[0], "architecture": f"{8 * ctypes.sizeof(ctypes.c_void_p)}-bit", "apis": {}}
    for dll in ("kernel32.dll", "psapi.dll", "pdh.dll", "advapi32.dll", "ntdll.dll"):
        try: ctypes.WinDLL(dll); result["apis"][dll] = True
        except OSError: result["apis"][dll] = False
    kernel = ctypes.WinDLL("kernel32.dll"); ntdll = ctypes.WinDLL("ntdll.dll")
    for name in ("GlobalMemoryStatusEx", "CreateJobObjectW", "AssignProcessToJobObject", "SetInformationJobObject", "QueryInformationJobObject", "SetPriorityClass", "GetPriorityClass", "SetProcessInformation"):
        result["apis"][name] = hasattr(kernel, name)
    for name in ("NtSetInformationProcess", "NtQueryInformationProcess"):
        result["apis"][name] = hasattr(ntdll, name)
    result["job_objects"] = bool(result["apis"].get("CreateJobObjectW"))
    result["monitoring"] = sys.platform == "win32"
    result["elevation_required_for_monitoring"] = False
    return result


def log_event(run_id: str, kind: str, detail: str, verified: bool = False, conn: sqlite3.Connection | None = None) -> None:
    own = conn is None; conn = conn or db()
    conn.execute("INSERT INTO events(run_id,timestamp,kind,detail,verified) VALUES(?,?,?,?,?)", (run_id, time.time(), kind, detail, int(verified)))
    if own: conn.commit(); conn.close()


def run_trial_isolated(preset: str, policy: str, calibration: dict[str, float] | None, cancel: threading.Event | None = None) -> dict[str, Any]:
    """Run a trial in a child process (see backend/trial_runner.py for why)."""
    stop = engine.native.StopEvent(rf"Local\SmartSwapCancel-{uuid.uuid4().hex}")
    proc = subprocess.Popen([sys.executable, "-m", "backend.trial_runner", preset, policy, json.dumps(calibration) if calibration else "-", stop.name],
                            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        while True:
            try: out, err = proc.communicate(timeout=0.5); break
            except subprocess.TimeoutExpired:
                if cancel is not None and cancel.is_set(): stop.set()
    finally:
        stop.close()
    if proc.returncode != 0 or not out.strip(): raise RuntimeError(f"trial process failed: {err.strip()[-400:]}")
    return json.loads(out)


def load_calibration(refresh: bool = False) -> dict[str, Any]:
    """Idle canary medians plus the idle foreground p95 used for interference validity."""
    if CALIBRATION_PATH.exists() and not refresh:
        data = json.loads(CALIBRATION_PATH.read_text(encoding="utf-8"))
        if data.get("engine_version") == 2: return data
    with TRIAL_LOCK:
        canary = engine.calibrate_canary()
        idle = run_trial_isolated("idle", "none", None)
    data = {"engine_version": 2, "measured_at": time.time(), "host": {"platform": sys.platform, "python": sys.version.split()[0], "cpu_count": os.cpu_count()},
            "canary_idle_median_ms": canary.as_dict(), "foreground_idle": idle["foreground"] and {k: v for k, v in idle["foreground"].items() if k.endswith("_ms") or k == "beats"},
            "memory": memory_metrics(), "recommended_tolerance_pct": RESPONSE_NOISE_TOLERANCE_PCT}
    CALIBRATION_PATH.parent.mkdir(parents=True, exist_ok=True); CALIBRATION_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return data


def run_engine_trial(run_id: str, preset: str, policy: str) -> None:
    """Execute one engine trial and persist it in the v1-compatible schema."""
    cancel = threading.Event()
    with ACTIVE_LOCK: ACTIVE[run_id] = {"cancel_event": cancel, "cancel": False}
    conn = None
    try:
        cal = load_calibration()
        engine.LIVE_MONITOR.stop()  # the live canary would add load to the trial
        with TRIAL_LOCK:
            result = run_trial_isolated(preset, policy, cal["canary_idle_median_ms"], cancel)
        f = result["foreground"]; idle_p95 = (cal.get("foreground_idle") or {}).get("p95_ms")
        if preset == "idle": validity = "CONTROL"
        elif f.get("p95_ms") is None or idle_p95 is None: validity = "UNKNOWN"
        elif policy != "none": validity = "MITIGATED"  # validity of a pair is judged on its unmitigated run
        else: validity = "VALID_INTERFERENCE" if f["p95_ms"] >= INTERFERENCE_MIN_INFLATION * idle_p95 else "INSUFFICIENT_INTERFERENCE"
        actions = [e for e in result["actions"] if not e["kind"].endswith("_revert")]
        rows = result["foreground_rows"]
        conn = db()
        conn.execute("UPDATE runs SET ended_at=?,foreground_ms=?,system_load=?,action_count=?,verified_count=?,action_types=?,recovery_observed=?,foreground_p50_ms=?,foreground_p95_ms=?,foreground_p99_ms=?,missed_deadlines=?,jitter_ms=?,operations=?,measurement_duration_ms=?,validity=?,background_throughput=?,engine_version=2,error=?,trace_json=?,background_json=?,time_to_first_action_s=? WHERE id=?",
                     (time.time(), f.get("mean_ms"), memory_metrics()["memory_load"], len(actions), sum(e["verified"] for e in actions), json.dumps(sorted({e["kind"] for e in actions})),
                      int(bool(result["reverts_verified"])), f.get("p50_ms"), f.get("p95_ms"), f.get("p99_ms"), f.get("deadline_misses"),
                      round(statistics.pstdev([r["sched"] for r in rows]), 3) if len(rows) > 1 else None, f.get("beats"), len(rows) * float(engine.FG_ENV["SS_INTERVAL"]) * 1000,
                      validity, sum(result["background_units_per_s"].values()) or None, "; ".join(result["errors"]) or None,
                      json.dumps(result["controller_trace"]), json.dumps(result["background_units_per_s"]), result["time_to_first_action_s"], run_id))
        for e in result["actions"]:
            log_event(run_id, e["kind"], f"t={e['t_s']}s value={e['value']} target={e['target']} {e['reason']}".strip(), e["verified"], conn)
        for seq, r in enumerate(rows):
            conn.execute("INSERT INTO latency_samples(run_id,seq,expected_ms,actual_ms,scheduling_delay_ms,execution_ms,deadline_missed) VALUES(?,?,?,?,?,?,?)",
                         (run_id, seq, (seq + 1) * float(engine.FG_ENV["SS_INTERVAL"]) * 1000, r["t"], r["sched"], r["cpu"] + r["mem"] + r["io"], int(r["sched"] + r["cpu"] + r["mem"] + r["io"] > engine.DEADLINE_MS)))
        if result["controller_trace"]:
            engaged = [s for s in result["controller_trace"] if s["engaged"]]
            log_event(run_id, "controller_summary", f"engaged {len(engaged)}/{len(result['controller_trace'])} periods; final cap {result['final_cap']}; time to first action {result['time_to_first_action_s']} s", True, conn)
        conn.commit()
    except Exception as exc:
        conn = conn or db()
        conn.execute("UPDATE runs SET ended_at=?, error=? WHERE id=?", (time.time(), f"{type(exc).__name__}: {exc}", run_id)); conn.commit()
    finally:
        if conn: conn.close()
        with ACTIVE_LOCK: ACTIVE.pop(run_id, None)


def normalize_preset(preset: str | None) -> str:
    preset = PRESET_ALIASES.get(preset or "cpu", preset or "cpu")
    if preset not in engine.PRESETS: raise HTTPException(400, "unknown preset")
    return preset


def _create_run(mode: str, policy: str, preset: str, batch_id: str, repetition: int | None, notes: str) -> str:
    run_id = str(uuid.uuid4())
    conn = db(); conn.execute("INSERT INTO runs(id,mode,started_at,repetition,batch_id,preset,policy,notes,engine_version) VALUES(?,?,?,?,?,?,?,?,2)", (run_id, mode, time.time(), repetition, batch_id, preset, policy, notes)); conn.commit(); conn.close()
    return run_id


@app.get("/api/health")
def health() -> dict[str, Any]: return {"status": "ok", "product": "SmartSwap", "version": app.version, "native_windows": sys.platform == "win32"}


@app.get("/api/capabilities")
def capabilities() -> dict[str, Any]: return native_capabilities()


@app.get("/api/metrics")
def metrics() -> dict[str, Any]:
    m = memory_metrics(); m["pressure"] = pressure(m); m["cpu_percent"] = psutil.cpu_percent(interval=None); return m


@app.get("/api/live")
def live_state() -> dict[str, Any]:
    """Observe-only Windows Stall Index of this desktop, plus what the controller would do."""
    return engine.LIVE_MONITOR.state()


@app.post("/api/live/start")
def live_start() -> dict[str, Any]:
    if TRIAL_LOCK.locked(): raise HTTPException(409, "a trial is running; live monitoring would disturb it")
    cal = load_calibration()
    engine.LIVE_MONITOR.start(Calibration(**cal["canary_idle_median_ms"]))
    return {"running": True}


@app.post("/api/live/stop")
def live_stop() -> dict[str, Any]:
    engine.LIVE_MONITOR.stop(); return {"running": False}


_PROC_CACHE: dict[int, psutil.Process] = {}


@app.get("/api/processes")
def processes(sort: str = "cpu") -> list[dict[str, Any]]:
    """Top processes. CPU % is measured since the previous call (psutil needs a cached handle)."""
    rows = []; seen = set()
    for proc in psutil.process_iter(["pid", "name"]):
        pid = proc.info["pid"]; seen.add(pid)
        cached = _PROC_CACHE.setdefault(pid, proc)
        try:
            with cached.oneshot():
                rows.append({"pid": pid, "name": proc.info["name"], "rss_mb": round(cached.memory_info().rss / 1048576, 1), "cpu_percent": round(cached.cpu_percent(None) / (os.cpu_count() or 1), 1)})
        except (psutil.NoSuchProcess, psutil.AccessDenied): continue
    for pid in set(_PROC_CACHE) - seen: _PROC_CACHE.pop(pid, None)
    rows = [r for r in rows if r["pid"] not in (0, 4)]  # idle + kernel pseudo-processes
    return sorted(rows, key=lambda x: x["cpu_percent" if sort == "cpu" else "rss_mb"], reverse=True)[:25]


@app.post("/api/experiments")
def start_experiment(payload: dict[str, Any]) -> dict[str, Any]:
    mode = payload.get("mode", "baseline")
    if mode not in MODE_POLICY: raise HTTPException(400, "mode must be baseline or optimized")
    policy = payload.get("policy") or MODE_POLICY[mode]
    if policy not in engine.POLICIES: raise HTTPException(400, "unknown policy")
    preset = normalize_preset(payload.get("preset"))
    run_id = _create_run(mode, policy, preset, payload.get("batch_id") or str(uuid.uuid4()), payload.get("repetition"), "SmartSwap v2 engine trial")
    threading.Thread(target=run_engine_trial, args=(run_id, preset, policy), daemon=True).start()
    return {"run_id": run_id, "mode": mode, "policy": policy, "preset": preset, "status": "running"}


def _run_paired_batch(batch_id: str, repetitions: int, preset: str, policy: str) -> None:
    try:
        for repetition in range(1, repetitions + 1):
            # Alternate the order (ABBA) so slow drift does not always favour one arm.
            arms = [("baseline", "none"), ("optimized", policy)]
            if repetition % 2 == 0: arms.reverse()
            for mode, arm_policy in arms:
                run_id = _create_run(mode, arm_policy, preset, batch_id, repetition, "Paired native comparison (alternating order)")
                run_engine_trial(run_id, preset, arm_policy)
    finally:
        ACTIVE_BATCHES.discard(batch_id)


@app.post("/api/paired-comparison")
def start_paired_comparison(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    repetitions = max(MIN_PAIRED_REPETITIONS, min(20, int(payload.get("repetitions", MIN_PAIRED_REPETITIONS))))
    preset = normalize_preset(payload.get("preset"))
    policy = payload.get("policy", "adaptive")
    if policy not in engine.POLICIES or policy == "none": raise HTTPException(400, "unknown policy")
    if ACTIVE_BATCHES: raise HTTPException(409, "another paired comparison is already running")
    batch_id = str(uuid.uuid4()); ACTIVE_BATCHES.add(batch_id)
    threading.Thread(target=_run_paired_batch, args=(batch_id, repetitions, preset, policy), daemon=True).start()
    return {"batch_id": batch_id, "repetitions": repetitions, "preset": preset, "policy": policy, "status": "running"}


@app.get("/api/presets")
def presets() -> list[dict[str, Any]]:
    return [{"id": key, "name": engine.PRESET_LABELS[key], "workers": [{"role": role, "count": count} for role, count in workers], "implemented": True, "primary_metric": "foreground p95 latency", "validity": f"unmitigated p95 >= {INTERFERENCE_MIN_INFLATION}x calibrated idle p95" if key != "idle" else "control: no background work"} for key, workers in engine.PRESETS.items()]


@app.get("/api/policies")
def policies() -> list[dict[str, Any]]:
    return [{"id": key, "description": text} for key, text in engine.POLICIES.items()]


@app.post("/api/calibration")
def calibration() -> dict[str, Any]:
    return load_calibration(refresh=True)


@app.get("/api/runs")
def runs() -> list[dict[str, Any]]:
    conn = db(); result = [dict(r) for r in conn.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT 200").fetchall()]; conn.close()
    for r in result: r.pop("trace_json", None)
    return result


@app.get("/api/runs/{run_id}")
def run_detail(run_id: str) -> dict[str, Any]:
    conn = db(); run = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone(); samples = [dict(r) for r in conn.execute("SELECT * FROM samples WHERE run_id=? ORDER BY timestamp", (run_id,)).fetchall()]; events = [dict(r) for r in conn.execute("SELECT * FROM events WHERE run_id=? ORDER BY timestamp", (run_id,)).fetchall()]; conn.close()
    if not run: raise HTTPException(404, "Run not found")
    run = dict(run); trace = json.loads(run.pop("trace_json") or "[]"); background = json.loads(run.pop("background_json") or "{}")
    return {"run": run, "samples": samples, "events": events, "trace": trace, "background_units_per_s": background}


@app.post("/api/experiments/{run_id}/cancel")
def cancel_experiment(run_id: str) -> dict[str, Any]:
    with ACTIVE_LOCK:
        if run_id not in ACTIVE: raise HTTPException(404, "Run is not active")
        ACTIVE[run_id]["cancel"] = True; ACTIVE[run_id]["cancel_event"].set()
    return {"run_id": run_id, "status": "cancellation_requested"}


@app.post("/api/cleanup")
def cleanup() -> dict[str, Any]:
    with ACTIVE_LOCK: active = list(ACTIVE.items())
    for run_id, item in active:
        item["cancel"] = True; item["cancel_event"].set()
        log_event(run_id, "emergency_cleanup", "Cancellation requested; owned workloads terminated", True)
    killed = engine.kill_all()
    return {"message": f"Emergency cleanup requested for {len(active)} run(s); {killed} SmartSwap-owned process(es) terminated.", "active_before_cleanup": len(active), "processes_terminated": killed}


ACTION_KINDS = {"priority_idle", "io_priority_very_low", "cpu_rate_cap", "job_assign", "foreground_above_normal"}


def paired_statistics(differences: list[float]) -> dict[str, Any]:
    """Two-sided paired tests on optimized-minus-baseline differences."""
    n = len(differences)
    if n < 2: return {"t_ci95_ms": None, "wilcoxon_p": None, "reliable": False}
    sd = statistics.stdev(differences); mean = statistics.fmean(differences)
    margin = t975(n - 1) * sd / math.sqrt(n)  # Student t, correct for every n (v1 used 1.96 for all n > 5)
    p = wilcoxon_signed_rank(differences)
    return {"t_ci95_ms": [round(mean - margin, 3), round(mean + margin, 3)], "wilcoxon_p": round(p, 4), "reliable": p < 0.05 and not (mean - margin <= 0 <= mean + margin)}


@app.get("/api/comparison")
def comparison() -> dict[str, Any]:
    conn = db()
    all_runs = [dict(r) for r in conn.execute("SELECT * FROM runs WHERE ended_at IS NOT NULL AND engine_version=2 AND error IS NULL ORDER BY started_at").fetchall()]
    groups: dict[str, list[dict[str, Any]]] = {}
    for run in all_runs: groups.setdefault(run.get("batch_id") or "unbatched", []).append(run)

    def pairs_of(rows: list[dict[str, Any]]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        by_rep: dict[Any, dict[str, dict[str, Any]]] = {}
        for r in rows: by_rep.setdefault(r.get("repetition"), {})[r["mode"]] = r
        return [(v["baseline"], v["optimized"]) for k, v in sorted(by_rep.items(), key=lambda kv: (kv[0] is None, kv[0] or 0)) if k is not None and "baseline" in v and "optimized" in v]

    eligible = [(key, rows) for key, rows in groups.items() if len(pairs_of(rows)) >= MIN_PAIRED_REPETITIONS]
    batch_id, selected = sorted(eligible, key=lambda item: max(r["started_at"] for r in item[1]))[-1] if eligible else ("none", [])
    pairs = pairs_of(selected)
    selected_ids = [r["id"] for r in selected]
    placeholders = ",".join("?" for _ in selected_ids) or "NULL"
    events = [dict(r) for r in conn.execute(f"SELECT run_id, kind, detail, verified FROM events WHERE run_id IN ({placeholders}) ORDER BY timestamp", selected_ids).fetchall()]
    conn.close()
    baseline_values = [float(b["foreground_p95_ms"]) for b, o in pairs]
    optimized_values = [float(o["foreground_p95_ms"]) for b, o in pairs]
    n = len(pairs)
    mean_baseline = statistics.fmean(baseline_values) if baseline_values else None
    mean_optimized = statistics.fmean(optimized_values) if optimized_values else None
    differences = [o - b for b, o in zip(baseline_values, optimized_values)]
    absolute = (mean_optimized - mean_baseline) if mean_baseline is not None and mean_optimized is not None else None
    percentage = (absolute / mean_baseline * 100) if absolute is not None and mean_baseline else None
    stddev = statistics.stdev(differences) if len(differences) > 1 else None
    tests = paired_statistics(differences)
    preset = selected[0].get("preset") if selected else None
    valid_pairs = n if preset == "idle" else sum(1 for b, _ in pairs if b.get("validity") == "VALID_INTERFERENCE")
    reliable = tests["reliable"]
    if n < MIN_PAIRED_REPETITIONS or absolute is None: classification = "INSUFFICIENT_DATA"
    elif valid_pairs < MIN_PAIRED_REPETITIONS: classification = "INSUFFICIENT_DATA"
    elif abs(percentage) <= RESPONSE_NOISE_TOLERANCE_PCT or not reliable: classification = "APPROXIMATELY_UNCHANGED"
    elif absolute < 0: classification = "IMPROVED"
    else: classification = "REGRESSED"
    action_counts: dict[str, int] = {}
    for e in events:
        if e["kind"] in ACTION_KINDS: action_counts[e["kind"]] = action_counts.get(e["kind"], 0) + 1
    reverts = [e for e in events if e["kind"].endswith("_revert")]
    throughput_retained = [o["background_throughput"] / b["background_throughput"] for b, o in pairs if b.get("background_throughput") and o.get("background_throughput") is not None]
    direction = "faster" if absolute is not None and absolute < 0 else "slower" if absolute is not None and absolute > 0 else "unchanged"
    if not n or percentage is None:
        conclusion = f"Run at least {MIN_PAIRED_REPETITIONS} paired repetitions before drawing a conclusion."
    elif valid_pairs < MIN_PAIRED_REPETITIONS:
        conclusion = f"Across {n} pair(s) the optimized p95 is {abs(percentage):.1f}% {direction}, but only {valid_pairs} baseline run(s) showed real interference (p95 >= {INTERFERENCE_MIN_INFLATION}x idle). No effectiveness claim is possible."
    elif classification == "APPROXIMATELY_UNCHANGED":
        conclusion = f"Across {n} valid pairs the optimized p95 is {abs(percentage):.1f}% {direction}; this is within noise or not statistically reliable (Wilcoxon p={tests['wilcoxon_p']})."
    else:
        conclusion = f"Across {n} valid pairs the optimized p95 is {abs(percentage):.1f}% {direction} (Wilcoxon p={tests['wilcoxon_p']}); the background kept a median {100 * statistics.median(throughput_retained):.0f}% of its throughput." if throughput_retained else f"Across {n} valid pairs the optimized p95 is {abs(percentage):.1f}% {direction} (Wilcoxon p={tests['wilcoxon_p']})."
    return {"batch_id": batch_id, "preset": preset, "policy": pairs[0][1].get("policy") if pairs else None, "metric": "foreground_p95_latency",
            "baseline": {"mean_ms": round(mean_baseline, 2) if mean_baseline is not None else None, "values_ms": [round(x, 2) for x in baseline_values]},
            "optimized": {"mean_ms": round(mean_optimized, 2) if mean_optimized is not None else None, "values_ms": [round(x, 2) for x in optimized_values]},
            "absolute_difference_ms": round(absolute, 2) if absolute is not None else None, "percentage_difference": round(percentage, 2) if percentage is not None else None,
            "direction": direction, "classification": classification, "paired_repetitions": n, "valid_paired_repetitions": valid_pairs,
            "standard_deviation_ms": round(stddev, 2) if stddev is not None else None, "noise_tolerance_pct": RESPONSE_NOISE_TOLERANCE_PCT,
            "statistically_reliable": reliable, "paired_tests": tests,
            "background_throughput_retained_median": round(statistics.median(throughput_retained), 3) if throughput_retained else None,
            "action_summary": {"unique_action_types": sorted(action_counts), "total_action_count": sum(action_counts.values()), "counts_by_type": action_counts,
                               "verified_actions": sum(1 for e in events if e["kind"] in ACTION_KINDS and e["verified"]),
                               "reverts": len(reverts), "verified_reverts": sum(1 for e in reverts if e["verified"]), "measurable_recovery_events": sum(1 for e in reverts if e["verified"])},
            "plain_language_conclusion": conclusion}


RUNNING_STUDY_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "SmartSwap" / "results"


@app.get("/api/study/progress")
def study_progress() -> dict[str, Any]:
    """Progress and latest trials of the most recent study still being written (outside the repo)."""
    dirs = sorted((p for p in RUNNING_STUDY_DIR.glob("*") if (p / "manifest.json").exists()), key=lambda p: p.stat().st_mtime) if RUNNING_STUDY_DIR.exists() else []
    if not dirs: return {"active": False}
    path = dirs[-1]; manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    lines = (path / "trials.jsonl").read_text(encoding="utf-8").splitlines() if (path / "trials.jsonl").exists() else []
    recent = []
    for line in lines[-10:][::-1]:
        t = json.loads(line)
        recent.append({"block": t["block"], "preset": t["preset"], "policy": t["policy"], "p95_ms": t["foreground"].get("p95_ms"), "actions": t["action_count"], "verified": t["actions_verified"], "errors": len(t["errors"]), "started_at": t["started_at"]})
    total = manifest["blocks"] * len(manifest["cells"])
    return {"active": len(lines) < total, "study_id": manifest["study_id"], "done": len(lines), "total": total, "host": manifest["host"], "recent": recent}


def latest_study_dir() -> Path | None:
    if not STUDY_DIR.exists(): return None
    studies = sorted((p for p in STUDY_DIR.iterdir() if (p / "analysis" / "summary.json").exists()), key=lambda p: p.stat().st_mtime)
    return studies[-1] if studies else None


@app.get("/api/study")
def study() -> dict[str, Any]:
    """The latest analysed block study (see smartswap/research)."""
    path = latest_study_dir()
    if not path: raise HTTPException(404, "No analysed study found. Run research.run_study then analyze.")
    return json.loads((path / "analysis" / "summary.json").read_text(encoding="utf-8"))


@app.get("/api/study/figures/{name}")
def study_figure(name: str) -> Response:
    path = latest_study_dir()
    target = (path / "analysis" / "figures" / name) if path else None
    if not target or not name.endswith(".png") or "/" in name or "\\" in name or not target.exists(): raise HTTPException(404, "figure not found")
    return Response(target.read_bytes(), media_type="image/png")


def make_pdf(lines: list[str]) -> bytes:
    safe = [line.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)')[:110] for line in lines[:42]]
    commands = ['BT', '/F1 16 Tf', '50 760 Td', '(SmartSwap validation report) Tj', '/F1 9 Tf', '0 -24 Td']
    for index, line in enumerate(safe):
        if index: commands.append('0 -15 Td')
        commands.append(f'({line}) Tj')
    commands.append('ET')
    stream = ('\n'.join(commands) + '\n').encode('latin-1', 'replace')
    objects = [
        b'<< /Type /Catalog /Pages 2 0 R >>',
        b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
        b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
        b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
        b'<< /Length ' + str(len(stream)).encode() + b' >>\nstream\n' + stream + b'endstream',
    ]
    output = bytearray(b'%PDF-1.4\n'); offsets = [0]
    for number, obj in enumerate(objects, 1):
        offsets.append(len(output)); output.extend(f'{number} 0 obj\n'.encode()); output.extend(obj); output.extend(b'\nendobj\n')
    xref = len(output); output.extend(f'xref\n0 {len(objects)+1}\n0000000000 65535 f \n'.encode())
    for offset in offsets[1:]: output.extend(f'{offset:010d} 00000 n \n'.encode())
    output.extend(f'trailer\n<< /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF'.encode())
    return bytes(output)


@app.get("/api/export/{kind}")
def export(kind: str) -> Response:
    conn = db(); runs_data = [dict(r) for r in conn.execute("SELECT * FROM runs ORDER BY started_at").fetchall()]; events = [dict(r) for r in conn.execute("SELECT * FROM events ORDER BY timestamp").fetchall()]; conn.close()
    if kind == "json": return Response(json.dumps({"runs": runs_data, "events": events}, indent=2), media_type="application/json", headers={"Content-Disposition":"attachment; filename=smartswap-report.json"})
    if kind == "csv":
        out=io.StringIO(); writer=csv.DictWriter(out, fieldnames=runs_data[0].keys() if runs_data else ["id","mode"]); writer.writeheader(); writer.writerows(runs_data); return Response(out.getvalue(), media_type="text/csv", headers={"Content-Disposition":"attachment; filename=smartswap-runs.csv"})
    if kind == "html": return HTMLResponse("<html><body><h1>SmartSwap report</h1><pre>" + json.dumps({"runs":runs_data,"events":events}, indent=2).replace("<", "&lt;") + "</pre></body></html>")
    if kind == "pdf":
        result = comparison(); action = result.get('action_summary', {}); tests = result.get('paired_tests', {})
        lines = [
            f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"Preset: {result.get('preset', '-')}; policy: {result.get('policy', '-')}; metric: {result.get('metric', '-')}",
            f"Baseline mean p95: {result.get('baseline', {}).get('mean_ms', '-')} ms",
            f"Optimized mean p95: {result.get('optimized', {}).get('mean_ms', '-')} ms",
            f"Difference: {result.get('absolute_difference_ms', '-')} ms ({result.get('percentage_difference', '-')}%)",
            f"Classification: {result.get('classification', '-')}",
            f"Paired repetitions: {result.get('paired_repetitions', 0)}; with real interference: {result.get('valid_paired_repetitions', 0)}",
            f"Paired t 95% CI of difference: {tests.get('t_ci95_ms')} ms; exact Wilcoxon p: {tests.get('wilcoxon_p')}",
            f"Background throughput retained (median): {result.get('background_throughput_retained_median', '-')}",
            f"Actions: {action.get('total_action_count', 0)} ({action.get('verified_actions', 0)} verified); reverts verified: {action.get('verified_reverts', 0)}/{action.get('reverts', 0)}",
            f"Conclusion: {result.get('plain_language_conclusion', '-')}",
        ]
        return Response(make_pdf(lines), media_type="application/pdf", headers={"Content-Disposition":"attachment; filename=smartswap-report.pdf"})
    raise HTTPException(400, "kind must be csv, json, html, or pdf")


if FRONTEND.exists(): app.mount("/", StaticFiles(directory=FRONTEND, html=True), name="frontend")
