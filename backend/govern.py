"""Governing applications SmartSwap did not start, and the blame study.

`ExternalGovernor` applies the blame module's nudges to arbitrary processes of
the current user. Its rules are stricter than the trial actuator's:
  - lowering only (IDLE priority class, very-low I/O priority); never a cap,
    never affinity, never a raise;
  - never the foreground window's application, SmartSwap itself, another
    user's process, or a PROTECTED_NAMES process (shell, compositor, audio,
    antivirus, session-critical processes);
  - optional allowlist of executable names (`allow`), so a user can opt in;
  - every change is verified by read-back, journaled to disk before it is
    made, and reverted on release, on stop, and on the next start after a
    crash (`recover`). A revert only restores a value if the process still has
    the value SmartSwap set, so a user's own later change is never overwritten.

`run_blame_trial` is the ground-truth experiment: it starts one culprit
application and several decoy applications (each a launcher process with
worker children), then lets a blame mode find and nudge the culprit among
*every* process on the desktop. Modes: none, oracle (nudge the culprit at
once; an upper bound), top_cpu, passive, interventional.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

import psutil

from . import native
from .blame import CALM_SNAPSHOT_EVERY, MODES, PROTECTED_NAMES, Blamer, BlameDecision, ProcSample, make_interrogator
from .controller import Calibration, Controller, ControllerConfig
from .engine import (CANARY_ENV, CONTROL_PERIOD_S, CPU_COUNT, DATA_DIR, FG_ENV, Ledger, _env, _reap, _spawn, ensure_data_files, summarize_rows)
from .probes import LAUNCHER, PROBE, WORKER

JOURNAL = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "SmartSwap" / "governor-journal.json"
HINT_VALUES = {"priority": (native.IDLE_PRIORITY_CLASS, native.get_priority_class, native.set_priority_class),
               "io_priority": (native.IO_PRIORITY_VERY_LOW, native.get_io_priority, native.set_io_priority)}
_USER: dict[tuple[int, float], bool] = {}
_ME = psutil.Process().username() if hasattr(psutil.Process(), "username") else None


def snapshot() -> list[ProcSample]:
    """Every visible process with cumulative CPU seconds and I/O bytes (no privileges needed)."""
    out = []
    for proc in psutil.process_iter(["pid", "ppid", "name", "create_time"]):
        info = proc.info
        try:
            with proc.oneshot():
                t = proc.cpu_times(); cpu = t.user + t.system
                try: c = proc.io_counters(); io = float(c.read_bytes + c.write_bytes)
                except (psutil.Error, AttributeError): io = 0.0
        except psutil.Error: continue
        key = (info["pid"], info["create_time"] or 0.0)
        if key not in _USER:
            try: _USER[key] = proc.username() == _ME
            except psutil.Error: _USER[key] = False
        out.append(ProcSample(info["pid"], info["ppid"] or 0, info["name"] or "?", cpu, io, info["create_time"] or 0.0, _USER[key]))
    if len(_USER) > 20000: _USER.clear()
    return out


class ExternalGovernor:
    """Verified, journaled, lowering-only hints on third-party applications."""

    def __init__(self, ledger: Ledger, journal: Path | None = JOURNAL, allow: set[str] | None = None, never: set[int] | None = None) -> None:
        self.ledger = ledger; self.journal = journal; self.allow = {a.lower() for a in allow} if allow else None
        self.never = set(never or ()) | {os.getpid()}
        self.applied: dict[int, dict[str, Any]] = {}   # app -> {"hint", "pids": {pid: [create_time, original]}}

    def _persist(self) -> None:
        if self.journal is None: return
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        self.journal.write_text(json.dumps({str(k): v for k, v in self.applied.items()}), encoding="utf-8")

    def allowed(self, pid: int) -> bool:
        try:
            p = psutil.Process(pid); name = p.name().lower()
            if pid in self.never or name in PROTECTED_NAMES or p.username() != _ME: return False
            fg = native.foreground_pid()
            if fg and (pid == fg or fg in {c.pid for c in p.children(recursive=True)}): return False
            return self.allow is None or name in self.allow
        except psutil.Error: return False

    def nudge(self, app: int, pids: list[int], hint: str, reason: str) -> bool:
        target, get, put = HINT_VALUES[hint]
        entry = self.applied.setdefault(app, {"hint": hint, "pids": {}})
        ok_all = True; touched = 0
        for pid in pids:
            if not self.allowed(pid): continue
            try: created = psutil.Process(pid).create_time()
            except psutil.Error: continue
            original = get(pid)
            if original is None or original == target or (hint == "priority" and original == native.IDLE_PRIORITY_CLASS): continue
            entry["pids"][str(pid)] = [created, original]; self._persist()  # journal before acting
            ok = put(pid, target); ok_all &= ok; touched += 1
        # Nothing to lower (already at or below the target, e.g. an IDLE-priority app) is a verified no-op.
        self.ledger.add(f"ext_{hint}_lower", f"app {app} ({touched} processes changed)", target, ok_all, reason)
        return ok_all

    def release(self, app: int, reason: str) -> bool:
        entry = self.applied.pop(app, None)
        if not entry: return True
        ok = _restore(entry); self._persist()
        self.ledger.add(f"ext_{entry['hint']}_revert", f"app {app} ({len(entry['pids'])} processes)", "original", ok, reason)
        return ok

    def release_all(self, reason: str = "stop") -> bool:
        return all([self.release(app, reason) for app in list(self.applied)])

    def apply(self, decision: BlameDecision, members: list[int]) -> None:
        if decision.kind == "nudge": self.nudge(decision.app, members, decision.hint, decision.reason)
        elif decision.kind == "release": self.release(decision.app, decision.reason)


def _restore(entry: dict[str, Any]) -> bool:
    target, get, put = HINT_VALUES[entry["hint"]]; ok = True
    for pid_s, (created, original) in entry["pids"].items():
        pid = int(pid_s)
        try:
            if psutil.Process(pid).create_time() != created: continue   # pid reused: not ours any more
        except psutil.Error: continue                                       # exited: nothing to restore
        if get(pid) != target: continue                                     # someone changed it since: respect that
        ok &= put(pid, original) and get(pid) == original
    return ok


def recover(journal: Path = JOURNAL) -> int:
    """Undo hints left behind by a crashed SmartSwap run; returns the number of applications restored."""
    if not journal.exists(): return 0
    try: entries = json.loads(journal.read_text(encoding="utf-8"))
    except (OSError, ValueError): return 0
    for entry in entries.values(): _restore(entry)
    journal.unlink(missing_ok=True)
    return len(entries)


# --- Blame study --------------------------------------------------------------
# Each application: (label, role, children, extra env). Exactly one is the culprit.
def scenarios() -> dict[str, dict[str, Any]]:
    n = CPU_COUNT
    light_cpu = ("light_cpu", "cpu", 1, {"SS_DUTY": "0.3"})
    trickle_io = ("trickle_io", "io", 1, {"SS_DUTY": "0.2", "SS_BG_IO_BLOCK": "65536"})
    return {
        # sanity: one saturating CPU application among light decoys
        "cpu": {"culprit": ("culprit_cpu", "cpu", n + n // 2, {}), "decoys": [light_cpu, trickle_io]},
        # resource mismatch: storage culprit, CPU-busy (but not saturating) decoy; Task-Manager's top-CPU is wrong
        "io_vs_cpu": {"culprit": ("culprit_io", "io", 16, {"SS_BG_IO_BLOCK": str(1 << 20)}), "decoys": [("busy_cpu", "cpu", max(1, n // 2), {}), light_cpu]},
        # a polite heavy application (IDLE priority, like an indexer or AV scan) next to a bandwidth culprit
        "membw_polite": {"culprit": ("culprit_membw", "membw", max(2, n // 2), {}), "decoys": [("polite_cpu", "cpu", n, {"SS_APP_PRIORITY": "idle"}), trickle_io]},
        # time-varying culprit next to a steady, non-saturating CPU user: correlation separates them
        "phased_cpu": {"culprit": ("culprit_phased", "cpu", n + n // 2, {"SS_ON_S": "3", "SS_OFF_S": "2"}), "decoys": [("steady_cpu", "cpu", max(1, n // 2), {}), trickle_io]},
    }


BLAME_MODES = ("none", "oracle", *MODES)
# Go/no-go comparison: blanket policies on every managed job vs. CausalShield (blame, then the minimal fix on the culprit only).
TRADEOFF_POLICIES = ("none", "nice_all", "ecoqos_all", "adaptive_all", "causalshield")
BLAME_FG_DURATION_S = 24.0  # long enough for up to 3 randomized rounds (about 5 s each) in every mode


def run_blame_trial(scenario: str, mode: str, calibration: Calibration, seed: int = 0, cancel: threading.Event | None = None, fingerprints: dict[str, Any] | None = None) -> dict[str, Any]:
    """A blame trial (`mode` in BLAME_MODES) or a trade-off trial (`mode` in TRADEOFF_POLICIES)."""
    from .engine import Actuator
    spec = scenarios().get(scenario)
    if spec is None: raise ValueError(f"unknown scenario {scenario}")
    if mode not in BLAME_MODES and mode not in TRADEOFF_POLICIES: raise ValueError(f"unknown blame mode {mode}")
    bg_file, probe_file = ensure_data_files(DATA_DIR)
    t0 = time.time(); ledger = Ledger(t0); errors: list[str] = []; stop = threading.Event()
    stop_event = native.StopEvent(rf"Local\SmartSwapBlame-{os.getpid()}-{int(t0 * 1e6)}")
    apps: list[dict[str, Any]] = []; canary = fg = None; canary_rows: list[dict[str, float]] = []; ctl_thread = None
    controller = Controller(calibration, float(CANARY_ENV["SS_INTERVAL"]) * 1000, ControllerConfig(act=(mode == "adaptive_all"), fingerprints=fingerprints))
    governor = ExternalGovernor(ledger, journal=None); blamer = interrogator = None
    group: Actuator | None = None; app_act: dict[int, Actuator] = {}; shield = None; ops_log: list[dict[str, Any]] = []; app_out: dict[str, Any] = {}
    fg_out: dict[str, Any] = {}; events: list[dict[str, Any]] = []; overhead_s = 0.0
    try:
        canary = _spawn(PROBE, _env({**CANARY_ENV, "SS_IO_FILE": str(probe_file), "SS_SEED": str(seed + 101)}))
        if canary.stdout.readline().strip() != "READY": raise RuntimeError("canary failed to start")
        threading.Thread(target=lambda: [canary_rows.append(json.loads(l)) for l in canary.stdout if l.startswith("{")], daemon=True).start()
        anchor = str(time.time())
        for culprit, (label, role, children, extra) in [(True, spec["culprit"])] + [(False, d) for d in spec["decoys"]]:
            env = _env({"SS_ROLE": role, "SS_CHILDREN": str(children), "SS_CHILD_CODE": WORKER, "SS_STOP_EVENT": stop_event.name, "SS_IO_FILE": str(bg_file),
                        "SS_BG_IO_BLOCK": str(1 << 20), "SS_BG_MEM_BYTES": str(64 << 20), "SS_SEED": str(seed * 1000 + len(apps)), "SS_T0": anchor, "SS_BG_MAX_DURATION": "90", **extra})
            apps.append({"label": label, "role": role, "children": children, "culprit": culprit, "proc": _spawn(LAUNCHER, env)})
        for a in apps:
            if a["proc"].stdout.readline().strip() != "READY": raise RuntimeError(f"{a['label']} failed: " + a["proc"].stderr.read()[-300:])
        t_ready = time.time()
        culprit_root = apps[0]["proc"].pid
        blamer = Blamer(CONTROL_PERIOD_S, engage_wsi=controller.cfg.engage_wsi, stop_pids=frozenset({os.getpid()}), exclude_pids=frozenset({canary.pid}))
        name = f"SmartSwap-{os.getpid()}-{int(t0 * 1000)}"
        group = Actuator([a["proc"].pid for a in apps], ledger, name, track_children=True); group.refresh()
        if mode == "nice_all": group.cpu_hint("static policy"); group.io_hint("static policy")
        if mode == "ecoqos_all": group.eco_hint("Efficiency mode"); group.cpu_hint("Efficiency mode")
        if mode == "causalshield":
            from .blame import FactorialInterrogator
            from .remedy import Optimizer
            from .shield import Shield, ladder_for
            shield = Shield(blamer, FactorialInterrogator(slo_wsi=controller.cfg.slo_wsi, seed=seed), Optimizer(slo_wsi=controller.cfg.slo_wsi, ladder=ladder_for("windows")))
            interrogator = shield.interrogator
            for a in apps:
                app_act[a["proc"].pid] = Actuator([a["proc"].pid], ledger, f"{name}-{a['proc'].pid}", track_children=True); app_act[a["proc"].pid].refresh()
        if mode in MODES: interrogator = make_interrogator(mode, slo_wsi=controller.cfg.slo_wsi, eval_periods=controller.cfg.benefit_eval_periods, min_reduction=controller.cfg.benefit_min_reduction, seed=seed)
        if mode == "oracle":
            members = [culprit_root] + [c.pid for c in psutil.Process(culprit_root).children(recursive=True)]
            hint = "io_priority" if apps[0]["role"] == "io" else "priority"
            governor.nudge(culprit_root, members, hint, "oracle: the culprit is known")
            events.append({"t_s": round(time.time() - t_ready, 3), "app": culprit_root, "label": apps[0]["label"], "kind": "nudge", "correct": True})

        def loop() -> None:
            nonlocal overhead_s
            label_of = {a["proc"].pid: a["label"] for a in apps}; last_snap = None; tick_n = 0
            while not stop.wait(CONTROL_PERIOD_S):
                tick = time.process_time()
                for d in controller.step((time.time() - t0) * 1000, canary_rows[-controller.cfg.window_beats:], None):
                    group.refresh(); group.apply(d)  # adaptive_all: the v2 ladder on every managed job
                row = controller.trace[-1]
                tick_n += 1
                if controller.engaged or tick_n % CALM_SNAPSHOT_EVERY == 0:  # full rate only while a stall is engaged
                    snap_t = time.time(); blamer.update(snapshot(), row["wsi"], row["excess"], native.foreground_pid(), elapsed_s=(snap_t - last_snap) if last_snap else None); last_snap = snap_t
                if shield is not None:
                    seen = len(interrogator.verdicts)
                    for op in shield.step(controller, row["attributed"] if row["attributed"] != "none" else controller.resource, blamer.names):
                        ops_log.append({"t_s": round(time.time() - t_ready, 3), "op": list(op)}); act = app_act.get(op[1]) if op[0] != "fallback" else None
                        if op[0] == "hint" and act:
                            act.refresh()
                            if op[3]: (act.cpu_hint if op[2] == "priority" else act.io_hint)("blame micro-experiment")
                            else: act.revert_hint(op[2], "blame micro-experiment")
                            if op[3]: events.append({"t_s": round(time.time() - t_ready, 3), "app": op[1], "label": label_of.get(op[1], "?"), "kind": "nudge", "correct": op[1] == culprit_root})
                        elif op[0] == "level" and act:
                            act.refresh()
                            if op[2] == "priority":
                                act.clear_cap("minimum intervention: priority")
                                if "eco" in act.active: act.revert_hint("eco", "minimum intervention: priority")
                            else:
                                if "eco" not in act.active: act.eco_hint("minimum intervention")
                                if op[2].startswith("cap"): act.set_cap(float(op[2][3:]), "minimum intervention")
                                else: act.clear_cap("minimum intervention: eco")
                        elif op[0] == "clear" and act:
                            act.clear_cap("culprit released")
                            if "eco" in act.active: act.revert_hint("eco", "culprit released")
                        elif op[0] == "fallback":
                            group.refresh(); group.cpu_hint("uncertainty fallback") if op[1] else group.revert_hint("priority", "uncertainty fallback")
                    for v in interrogator.verdicts[seen:]:
                        events.append({"t_s": round(time.time() - t_ready, 3), "app": v["app"], "label": label_of.get(v["app"], "?"), "kind": "confirm" if v["verdict"] == "confirmed" else "exonerate", "correct": v["app"] == culprit_root})
                elif interrogator is not None:
                    ranked = blamer.top_cpu() if mode == "top_cpu" else blamer.rank(row["attributed"] if row["attributed"] != "none" else controller.resource)
                    seen = len(interrogator.verdicts)
                    for d in interrogator.step(controller.engaged, controller.resource, controller.wsi_history, ranked, blamer.names):
                        governor.apply(d, blamer.members.get(d.app, [d.app]) + _descendants(d.app))
                        events.append({"t_s": round(time.time() - t_ready, 3), "app": d.app, "label": label_of.get(d.app, blamer.names.get(d.app, "?")), "kind": d.kind, "correct": d.app == culprit_root, "reason": d.reason})
                    for v in interrogator.verdicts[seen:]:
                        events.append({"t_s": round(time.time() - t_ready, 3), "app": v["app"], "label": label_of.get(v["app"], "?"), "kind": "confirm" if v["verdict"] == "confirmed" else "exonerate" if v["verdict"] == "exonerated" else "blame", "correct": v["app"] == culprit_root})
                row["suspects"] = [s.as_dict() | {"label": label_of.get(s.app)} for s in blamer.rank(row["attributed"])[:3]] if row["attributed"] in ("cpu", "mem", "io") else []
                overhead_s += time.process_time() - tick
        ctl_thread = threading.Thread(target=loop, daemon=True); ctl_thread.start()

        fg = _spawn(PROBE, _env({**FG_ENV, "SS_DURATION": str(BLAME_FG_DURATION_S), "SS_IO_FILE": str(probe_file), "SS_SEED": str(seed + 7)}))
        deadline = time.time() + 90
        while True:
            try: out, err = fg.communicate(timeout=0.25); break
            except Exception:
                if cancel is not None and cancel.is_set(): raise RuntimeError("cancelled")
                if time.time() > deadline: raise RuntimeError("foreground probe timed out")
        if fg.returncode != 0: raise RuntimeError("foreground probe failed: " + err[-300:])
        fg_out = json.loads(out.strip().splitlines()[-1])
    except Exception as exc:
        errors.append(f"{type(exc).__name__}: {exc}")
    finally:
        stop.set()
        if ctl_thread: ctl_thread.join(timeout=3)
        governor.release_all("end of trial")
        for act in app_act.values(): act.revert_all(set())
        if group: group.revert_all(set())
        stop_event.set()
        for a in apps:
            try: app_out[a["label"]] = json.loads(a["proc"].communicate(timeout=20)[0].strip().splitlines()[-1])
            except Exception as exc: errors.append(f"{a['label']}: {type(exc).__name__}: {exc}")
            _reap(a["proc"])
        _reap(canary); _reap(fg); stop_event.close()

    rows = fg_out.get("rows", [])
    nudges = [e for e in events if e["kind"] == ("confirm" if mode in ("interventional", "factorial", "causalshield") else "nudge")]  # what each mode actually blamed
    win = (fg_out.get("wall_start"), fg_out.get("wall_end")); app_units = {}
    if win[0] and win[1]:
        for label, o in app_out.items():
            app_units[label] = round(sum(n for b, n in o.get("buckets", {}).items() if int(win[0] * 10) <= int(b) < int(win[1] * 10)) / (win[1] - win[0]), 3)
    verdicts = interrogator.verdicts if interrogator else []
    first_correct = next((e["t_s"] for e in nudges if e["correct"]), None)
    culprit_label = apps[0]["label"] if apps else None; culprit_pid = apps[0]["proc"].pid if apps else None
    return {
        "scenario": scenario, "mode": mode, "seed": seed, "started_at": t0, "errors": errors,
        "foreground": summarize_rows(rows, float(FG_ENV["SS_INTERVAL"]) * 1000, fg_out.get("duration_ms", 0.0)), "foreground_rows": rows,
        "apps": [{k: v for k, v in a.items() if k != "proc"} | {"pid": a["proc"].pid} for a in apps], "culprit": culprit_label,
        "events": events, "verdicts": [v | {"correct": v["app"] == culprit_pid} for v in verdicts],
        "first_nudge_correct": nudges[0]["correct"] if nudges else None, "wrong_nudges": sum(1 for e in nudges if not e["correct"]),
        "time_to_correct_nudge_s": first_correct, "nudges": len(nudges), "probe_nudges": sum(1 for e in events if e["kind"] == "nudge"),
        "actions": ledger.entries, "actions_verified": sum(e["verified"] for e in ledger.entries if not e["kind"].endswith("_revert")),
        "action_count": sum(1 for e in ledger.entries if not e["kind"].endswith("_revert")),
        "reverts_verified": all(e["verified"] for e in ledger.entries if e["kind"].endswith("_revert")) if any(e["kind"].endswith("_revert") for e in ledger.entries) else None,
        "controller_trace": controller.trace, "governor_cpu_share": round(overhead_s / max(1e-3, time.time() - t0), 5),
        "calibration": asdict(calibration), "fingerprints": fingerprints, "app_units_per_s": app_units,
        "shield": (shield.summary() | {"ops": ops_log}) if shield else None,
    }


def _descendants(pid: int) -> list[int]:
    try: return [c.pid for c in psutil.Process(pid).children(recursive=True)]
    except psutil.Error: return []
