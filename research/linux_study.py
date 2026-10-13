"""Cross-OS validation on Linux: the same canary, controller, and blame logic,
checked against the kernel's own stall metric (PSI).

    python -m research.linux_study run --design validity   --blocks 8
    python -m research.linux_study run --design mitigation --blocks 8
    python -m research.linux_study run --design blame      --blocks 6
    python -m research.linux_study analyze <study_dir>

Designs
    validity    presets {idle, cpu, membw, io, mixed} x {none, observe}. Every
                control period records the canary's WSI next to PSI "some"
                (cpu, io, memory) over the same 200 ms. Questions: does the
                unprivileged canary track the kernel's stall accounting, and
                which of the two better predicts a separate app's p95?
    mitigation  presets {cpu, membw, io, mixed} x {none, nice, adaptive}; the
                ladder is nice 19 -> ioprio IDLE -> cgroup CPU quota (AIMD).
    blame       govern.scenarios() x {none, oracle, top_cpu, passive,
                interventional}; nudges are nice 19 / ioprio IDLE. Only
                processes this harness started may be governed; every other
                process on the machine is still ranked (and marked protected).

Trials use the Windows harness's JSONL schema, so research.analyze /
research.designs work on them unchanged. Needs root only for the cgroup cap
(the adaptive policy's level 2); everything else is unprivileged.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import random
import statistics
import subprocess
import sys
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

import psutil

from backend import posix
from backend.blame import MODES, Blamer, make_interrogator
from backend.controller import Calibration, Controller, ControllerConfig, learn_fingerprint
from backend.engine import CANARY_ENV, CONTROL_PERIOD_S, FG_ENV, Ledger, summarize_rows
from backend.govern import scenarios
from backend.stats import rank, spearman

N = os.cpu_count() or 4
DATA = Path(os.environ.get("SS_DATA", Path.home() / ".smartswap"))
PRESETS = {"idle": [], "cpu": [("cpu", 2 * N)], "membw": [("membw", max(2, N // 2))], "io": [("io", 16)],
           "mixed": [("cpu", N), ("membw", max(1, N // 4)), ("io", 4)]}
BLAME_MODES = ["none", "oracle", *MODES]
FG_BLAME_S = 14.0


def cells(design: str) -> list[tuple[str, str]]:
    if design == "validity": return [(p, q) for p in PRESETS for q in ("none", "observe")]
    if design == "mitigation": return [(p, q) for p in ("cpu", "membw", "io", "mixed") for q in ("none", "nice", "adaptive")]
    if design == "blame": return [(s, m) for s in scenarios() for m in BLAME_MODES]
    raise ValueError(design)


def data_files() -> tuple[Path, Path]:
    DATA.mkdir(parents=True, exist_ok=True); out = []
    for name, mb in (("bg.bin", 1024), ("probe.bin", 64)):
        path = DATA / name
        if not path.exists() or path.stat().st_size != mb << 20:
            with open(path, "wb") as f:
                for _ in range(mb): f.write(os.urandom(1 << 20))
        out.append(path)
    return out[0], out[1]


def spawn(code: str, env: dict[str, str]) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", code], env={**os.environ, **env}, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def reap(proc: subprocess.Popen | None) -> None:
    if proc is None: return
    try:
        if proc.poll() is None: proc.kill()
        proc.wait(timeout=5)
    except Exception: pass


def calibrate(seconds: float = 4.0) -> Calibration:
    _, probe = data_files()
    p = spawn(posix.PROBE, {**CANARY_ENV, "SS_STREAM": "0", "SS_DURATION": str(seconds), "SS_IO_FILE": str(probe)})
    out, _ = p.communicate(timeout=seconds + 30)
    return Calibration.from_rows(json.loads(out.strip().splitlines()[-1])["rows"])


def learn_fingerprints(cal: Calibration, seconds: float = 3.0) -> dict[str, list[float] | None]:
    """Generate each kind of contention briefly and record the canary's response (see controller.py)."""
    bg, probe = data_files(); out: dict[str, list[float] | None] = {}
    for resource, preset in (("cpu", "cpu"), ("mem", "membw"), ("io", "io")):
        stop_file = DATA / f"fp-stop-{os.getpid()}-{resource}"; stop_file.unlink(missing_ok=True); workers = []
        try:
            for role, count in PRESETS[preset]:
                for j in range(count): workers.append(spawn(posix.WORKER, {"SS_ROLE": role, "SS_STOP_FILE": str(stop_file), "SS_IO_FILE": str(bg), "SS_BG_IO_BLOCK": str(1 << 20), "SS_BG_MEM_BYTES": str(64 << 20), "SS_SEED": str(j)}))
            for w in workers: w.stdout.readline()
            c = spawn(posix.PROBE, {**CANARY_ENV, "SS_STREAM": "0", "SS_DURATION": str(seconds), "SS_IO_FILE": str(probe)})
            rows = json.loads(c.communicate(timeout=seconds + 30)[0].strip().splitlines()[-1])["rows"]
            out[resource] = learn_fingerprint([rows[i:i + 8] for i in range(0, len(rows) - 7, 4)], cal)
        finally:
            stop_file.touch()
            for w in workers:
                try: w.communicate(timeout=20)
                except Exception: pass
                reap(w)
            stop_file.unlink(missing_ok=True)
    return out


class Actuator:
    """nice / ioprio / cgroup quota on the processes this harness started; verified, reverted."""

    def __init__(self, pids: list[int], ledger: Ledger, name: str) -> None:
        self.pids = pids; self.ledger = ledger; self.name = name; self.cap: posix.CpuCap | None = None; self.active: set[str] = set()
        self.orig_nice = {p: posix.get_nice(p) for p in pids}; self.orig_io = {p: posix.get_ioprio(p) for p in pids}

    def _tree(self) -> list[int]:
        out = list(self.pids)
        for p in self.pids:
            try: out += [c.pid for c in psutil.Process(p).children(recursive=True)]
            except psutil.Error: pass
        return out

    def hint(self, kind: str, reason: str, pids: list[int] | None = None) -> bool:
        targets = pids or self._tree(); self.active.add(kind)
        for p in targets:
            self.orig_nice.setdefault(p, posix.get_nice(p)); self.orig_io.setdefault(p, posix.get_ioprio(p))
        ok = all([posix.set_nice(p, posix.NICE_IDLE) if kind == "priority" else posix.set_ioprio(p, posix.ioprio_idle()) for p in targets])
        self.ledger.add("nice_19" if kind == "priority" else "ioprio_idle", f"{len(targets)} processes", None, ok, reason)
        return ok

    def revert(self, kind: str, reason: str, pids: list[int] | None = None) -> bool:
        self.active.discard(kind); targets = [p for p in (pids or self._tree()) if psutil.pid_exists(p)]
        if kind == "priority": ok = all([posix.set_nice(p, self.orig_nice.get(p) or 0) for p in targets])
        else: ok = all([posix.set_ioprio(p, self.orig_io.get(p) or 0) for p in targets])
        self.ledger.add("nice_revert" if kind == "priority" else "ioprio_revert", f"{len(targets)} processes", None, ok, reason)
        return ok

    def set_cap(self, percent: float, reason: str) -> None:
        if self.cap is None:
            self.cap = posix.CpuCap(self.name); members = sum(self.cap.assign(p) for p in self._tree())
            self.ledger.add("cgroup_assign", f"{members} processes", members, self.cap.ok and members > 0)
        self.ledger.add("cpu_quota_cap", "cgroup", percent, self.cap.set_percent(percent), reason)

    def clear_cap(self, reason: str) -> None:
        if self.cap is not None and self.cap.percent is not None: self.ledger.add("cpu_quota_revert", "cgroup", None, self.cap.clear(), reason)

    def apply(self, d: Any) -> None:
        if d.kind == "hint" and d.resource in ("priority", "io_priority"): self.hint(d.resource, d.reason)
        elif d.kind == "hint_revert" and d.resource in ("priority", "io_priority"): self.revert(d.resource, d.reason)
        elif d.kind == "cap_set": self.set_cap(d.value, d.reason)
        elif d.kind == "cap_clear": self.clear_cap(d.reason)

    def revert_all(self) -> None:
        self.clear_cap("end of trial")
        for kind in sorted(self.active): self.revert(kind, "end of trial")
        if self.cap: self.cap.close()


def run_trial(design: str, preset: str, policy: str, cal: Calibration, seed: int, fingerprints: dict[str, Any] | None = None) -> dict[str, Any]:
    bg, probe = data_files(); t0 = time.time(); ledger = Ledger(t0); errors: list[str] = []
    stop_file = DATA / f"stop-{os.getpid()}-{int(t0 * 1e6)}"; stop = threading.Event()
    canary = fg = None; rows: list[dict[str, float]] = []; workers: list[tuple[str, subprocess.Popen]] = []; apps: list[dict[str, Any]] = []
    cfg = ControllerConfig(act=(policy == "adaptive"), fingerprints=fingerprints); controller = Controller(cal, float(CANARY_ENV["SS_INTERVAL"]) * 1000, cfg)
    actuator: Actuator | None = None; interrogator = None; blamer = None; events: list[dict[str, Any]] = []; ctl = None; fg_out: dict[str, Any] = {}; overhead = 0.0
    blame = design == "blame"; use_canary = blame or policy in ("observe", "adaptive") or design == "validity"
    try:
        if use_canary:
            canary = spawn(posix.PROBE, {**CANARY_ENV, "SS_IO_FILE": str(probe), "SS_SEED": str(seed + 101)})
            if canary.stdout.readline().strip() != "READY": raise RuntimeError("canary failed: " + canary.stderr.read()[-300:])
            threading.Thread(target=lambda: [rows.append(json.loads(l)) for l in canary.stdout if l.startswith("{")], daemon=True).start()
        common = {"SS_STOP_FILE": str(stop_file), "SS_IO_FILE": str(bg), "SS_BG_IO_BLOCK": str(1 << 20), "SS_BG_MEM_BYTES": str(64 << 20), "SS_BG_MAX_DURATION": "90"}
        if blame:
            spec = scenarios()[preset]; anchor = str(time.time())
            for culprit, (label, role, children, extra) in [(True, spec["culprit"])] + [(False, d) for d in spec["decoys"]]:
                env = {**common, "SS_ROLE": role, "SS_CHILDREN": str(children), "SS_CHILD_CODE": posix.WORKER, "SS_SEED": str(seed * 1000 + len(apps)), "SS_T0": anchor, **extra}
                apps.append({"label": label, "role": role, "children": children, "culprit": culprit, "proc": spawn(posix.LAUNCHER, env)})
            for a in apps:
                if a["proc"].stdout.readline().strip() != "READY": raise RuntimeError(f"{a['label']} failed: " + a["proc"].stderr.read()[-300:])
        else:
            for role, count in PRESETS[preset]:
                for _ in range(count): workers.append((role, spawn(posix.WORKER, {**common, "SS_ROLE": role, "SS_SEED": str(seed * 1000 + len(workers))})))
            for role, p in workers:
                if p.stdout.readline().strip() != "READY": raise RuntimeError(f"{role} worker failed: " + p.stderr.read()[-300:])
        t_ready = time.time(); pids = [p.pid for _, p in workers] + [a["proc"].pid for a in apps]
        if pids: actuator = Actuator(pids, ledger, f"smartswap-{os.getpid()}-{int(t0 * 1000)}")
        if policy == "nice" and actuator: actuator.hint("priority", "static policy"); actuator.hint("io_priority", "static policy")
        culprit_root = apps[0]["proc"].pid if apps else None
        if blame:
            mine = {os.getpid()} | set(pids)
            blamer = Blamer(CONTROL_PERIOD_S, engage_wsi=cfg.engage_wsi, stop_pids=frozenset({os.getpid()}), exclude_pids=frozenset({canary.pid}))
            if policy in MODES: interrogator = make_interrogator(policy, slo_wsi=cfg.slo_wsi, eval_periods=cfg.benefit_eval_periods, min_reduction=cfg.benefit_min_reduction, seed=seed)
            if policy == "oracle":
                kind = "io_priority" if apps[0]["role"] == "io" else "priority"
                tree = [culprit_root] + [c.pid for c in psutil.Process(culprit_root).children(recursive=True)]
                actuator.hint(kind, "oracle: the culprit is known", tree)
                events.append({"t_s": 0.0, "app": culprit_root, "label": apps[0]["label"], "kind": "nudge", "correct": True})
        nudged: dict[int, tuple[str, list[int]]] = {}

        def loop() -> None:
            nonlocal overhead
            from backend.govern import snapshot
            last = posix.psi_totals(); last_t = time.time(); label_of = {a["proc"].pid: a["label"] for a in apps}
            while not stop.wait(CONTROL_PERIOD_S):
                tick = time.process_time(); now = posix.psi_totals(); now_t = time.time()
                psi = {k: round(min(100.0, 100 * (now[k] - last.get(k, now[k])) / 1e6 / max(1e-3, now_t - last_t)), 3) for k in now}; last, last_t = now, now_t
                if not rows: continue
                for d in controller.step((now_t - t0) * 1000, rows[-cfg.window_beats:], None):
                    if actuator: actuator.apply(d)
                row = controller.trace[-1]; row["psi"] = psi
                if blamer is not None:
                    snap = snapshot(); outside = {p.pid for p in snap if p.pid not in mine and psutil.pid_exists(p.pid) and not _descends(p.pid, mine)}
                    blamer.update(snap, row["wsi"], row["excess"])
                    for app in list(blamer.protected): blamer.protected[app] = blamer.protected[app] or app in outside
                    if interrogator is not None:
                        ranked = blamer.top_cpu() if policy == "top_cpu" else blamer.rank(row["attributed"] if row["attributed"] != "none" else controller.resource)
                        seen = len(interrogator.verdicts)
                        for d in interrogator.step(controller.engaged, controller.resource, controller.wsi_history, ranked, blamer.names):
                            if d.kind == "nudge":
                                tree = [d.app] + [c.pid for c in psutil.Process(d.app).children(recursive=True)] if psutil.pid_exists(d.app) else []
                                tree = [p for p in tree if p in mine or _descends(p, mine)]  # never touch processes we did not start
                                if tree: actuator.hint(d.hint, d.reason, tree)
                                nudged[d.app] = (d.hint, tree)
                            elif d.app in nudged:
                                hint, tree = nudged.pop(d.app)
                                if tree: actuator.revert(hint, d.reason, tree)
                            events.append({"t_s": round(time.time() - t_ready, 3), "app": d.app, "label": label_of.get(d.app, blamer.names.get(d.app, "?")), "kind": d.kind, "correct": d.app == culprit_root, "reason": d.reason})
                        for v in interrogator.verdicts[seen:]:
                            events.append({"t_s": round(time.time() - t_ready, 3), "app": v["app"], "label": label_of.get(v["app"], "?"), "kind": "confirm" if v["verdict"] == "confirmed" else "exonerate" if v["verdict"] == "exonerated" else "blame", "correct": v["app"] == culprit_root})
                overhead += time.process_time() - tick
        ctl = threading.Thread(target=loop, daemon=True); ctl.start()
        fg = spawn(posix.PROBE, {**FG_ENV, "SS_DURATION": str(FG_BLAME_S if blame else float(FG_ENV["SS_DURATION"])), "SS_IO_FILE": str(probe), "SS_SEED": str(seed + 7)})
        out, err = fg.communicate(timeout=120)
        if fg.returncode != 0: raise RuntimeError("foreground failed: " + err[-300:])
        fg_out = json.loads(out.strip().splitlines()[-1])
    except Exception as exc:
        errors.append(f"{type(exc).__name__}: {exc}")
    finally:
        stop.set()
        if ctl: ctl.join(timeout=3)
        if actuator: actuator.revert_all()
        stop_file.touch()
        outs = []
        for p in [p for _, p in workers] + [a["proc"] for a in apps]:
            try:
                o, _ = p.communicate(timeout=30); outs.append(json.loads(o.strip().splitlines()[-1]))
            except Exception as exc: errors.append(f"worker: {type(exc).__name__}: {exc}")
            reap(p)
        reap(canary); reap(fg); stop_file.unlink(missing_ok=True)
    fg_rows = fg_out.get("rows", []); win = (fg_out.get("wall_start"), fg_out.get("wall_end")); tput: dict[str, float] = {}
    if win[0] and win[1]:
        for o in outs:
            n = sum(v for b, v in o.get("buckets", {}).items() if int(win[0] * 10) <= int(b) < int(win[1] * 10))
            tput[o["role"]] = tput.get(o["role"], 0) + n / (win[1] - win[0])
    acts = [e for e in ledger.entries if not e["kind"].endswith("_revert")]; reverts = [e for e in ledger.entries if e["kind"].endswith("_revert")]
    nudges = blames(events, policy)
    result = {"preset": preset, "policy": policy, "seed": seed, "started_at": t0, "errors": errors,
              "foreground": summarize_rows(fg_rows, float(FG_ENV["SS_INTERVAL"]) * 1000, fg_out.get("duration_ms", 0.0)), "foreground_rows": fg_rows,
              "background_units_per_s": {k: round(v, 3) for k, v in tput.items()}, "actions": ledger.entries, "action_count": len(acts),
              "actions_verified": sum(e["verified"] for e in acts), "reverts_verified": all(e["verified"] for e in reverts) if reverts else None,
              "controller_trace": controller.trace if use_canary else [], "canary_beats": len(rows), "calibration": asdict(cal),
              "controller_engaged_share": round(sum(1 for s in controller.trace if s["engaged"]) / len(controller.trace), 3) if controller.trace else None,
              "fingerprints": fingerprints, "time_to_first_action_s": None, "control_cpu_share": round(overhead / max(1e-3, time.time() - t0), 5)}
    if blame:
        result.update(scenario=preset, mode=policy, culprit=apps[0]["label"] if apps else None, events=events,
                      apps=[{k: v for k, v in a.items() if k != "proc"} | {"pid": a["proc"].pid} for a in apps],
                      verdicts=[v | {"correct": v["app"] == (apps[0]["proc"].pid if apps else None)} for v in (interrogator.verdicts if interrogator else [])],
                      first_nudge_correct=nudges[0]["correct"] if nudges else None, wrong_nudges=sum(1 for e in nudges if not e["correct"]),
                      time_to_correct_nudge_s=next((e["t_s"] for e in nudges if e["correct"]), None), nudges=len(nudges),
                      probe_nudges=sum(1 for e in events if e["kind"] == "nudge"), governor_cpu_share=result["control_cpu_share"])
    return result


def blames(events: list[dict[str, Any]], mode: str) -> list[dict[str, Any]]:
    """The applications a mode actually blamed, in order: a confirmation for the experimental
    modes (their test nudges are probes, not verdicts), every kept nudge for the heuristics."""
    return [e for e in events if e["kind"] == ("confirm" if mode in ("interventional", "factorial") else "nudge")]


def _descends(pid: int, roots: set[int]) -> bool:
    try:
        for parent in psutil.Process(pid).parents():
            if parent.pid in roots: return True
    except psutil.Error: pass
    return False


def host() -> dict[str, Any]:
    try: model = next(l.split(":", 1)[1].strip() for l in Path("/proc/cpuinfo").read_text().splitlines() if l.startswith("model name"))
    except (OSError, StopIteration): model = platform.processor()
    return {"os": platform.platform(), "python": sys.version.split()[0], "cpu": model, "logical_cpus": N, "ram_gb": round(psutil.virtual_memory().total / 2**30, 1),
            "virtualized": Path("/sys/hypervisor").exists() or "hypervisor" in Path("/proc/cpuinfo").read_text(), "psi": bool(posix.psi_totals())}


def run(args: argparse.Namespace) -> Path:
    study_id = args.study_id or time.strftime(f"linux-{args.design}-%Y%m%d-%H%M%S"); out = args.out / study_id; out.mkdir(parents=True, exist_ok=True)
    trials = out / "trials.jsonl"; manifest = out / "manifest.json"
    if not manifest.exists():
        manifest.write_text(json.dumps({"study_id": study_id, "design_name": args.design, "platform": "linux", "seed": args.seed, "blocks": args.blocks, "cells": cells(args.design),
                                        "presets": PRESETS, "foreground": FG_ENV, "canary": CANARY_ENV, "control_period_s": CONTROL_PERIOD_S, "controller": ControllerConfig().__dict__,
                                        "host": host(), "started": time.strftime("%Y-%m-%dT%H:%M:%S%z")}, indent=2), encoding="utf-8")
    done = set()
    if trials.exists():
        for l in trials.read_text().splitlines():
            if l.strip(): r = json.loads(l); done.add((r["block"], r["preset"], r["policy"]))
    rng = random.Random(args.seed); total = args.blocks * len(cells(args.design)); count = len(done)
    for block in range(args.blocks):
        order = cells(args.design); rng.shuffle(order)
        if all((block, p, q) in done for p, q in order): continue
        time.sleep(2); cal = calibrate(); fps = learn_fingerprints(cal) if args.fingerprint else None
        if fps: print(f"block {block} fingerprints: {fps}", flush=True)
        for pos, (p, q) in enumerate(order):
            if (block, p, q) in done: continue
            load = psutil.cpu_percent(interval=1.0)
            r = run_trial(args.design, p, q, cal, block * 100 + pos, fps); r.update(block=block, position=pos, study_id=study_id, host_cpu_pct_before=load)
            with trials.open("a") as f: f.write(json.dumps(r) + "\n")
            count += 1
            print(f"[{count}/{total}] b{block} {p:12} {q:14} p95={r['foreground'].get('p95_ms')} acts={r['action_count']} err={r['errors'][:1]}", flush=True)
            time.sleep(0.5)
    return out


# --- analysis -------------------------------------------------------------------
def auc(pos: list[float], neg: list[float]) -> float | None:
    if not pos or not neg: return None
    r = rank(pos + neg); return (sum(r[: len(pos)]) - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg))


def analyse_validity(study: Path) -> dict[str, Any]:
    trials = [json.loads(l) for l in (study / "trials.jsonl").read_text().splitlines() if l.strip()]
    obs = [t for t in trials if t["policy"] == "observe" and not t["errors"] and t["controller_trace"]]
    periods = [(t["preset"], r) for t in obs for r in t["controller_trace"] if "psi" in r]
    psi_sum = lambda r: min(100.0, r["psi"].get("cpu", 0) + r["psi"].get("io", 0) + r["psi"].get("mem", 0))
    wsi = [r["wsi"] for _, r in periods]; psi = [psi_sum(r) for _, r in periods]
    per_period = spearman(wsi, psi, permutations=1000) if len(periods) >= 10 else (None, None)
    by_res = {}
    for res, key in (("cpu", "cpu"), ("io", "io")):
        x = [r["excess"][res] for _, r in periods]; y = [r["psi"].get(key, 0) for _, r in periods]
        by_res[res] = spearman(x, y, permutations=1000)[0] if len(periods) >= 10 else None
    detect = {}
    idle_w = [r["wsi"] for p, r in periods if p == "idle"]; idle_p = [psi_sum(r) for p, r in periods if p == "idle"]
    for preset in ("cpu", "membw", "io", "mixed"):
        detect[preset] = {"canary_auc": auc([r["wsi"] for p, r in periods if p == preset], idle_w), "psi_auc": auc([psi_sum(r) for p, r in periods if p == preset], idle_p),
                          "canary_wsi_median": statistics.median([r["wsi"] for p, r in periods if p == preset] or [float("nan")]),
                          "psi_some_median": {k: statistics.median([r["psi"].get(k, 0) for p, r in periods if p == preset] or [float("nan")]) for k in ("cpu", "io", "mem")}}
    trial_pts = [(statistics.fmean(r["wsi"] for r in t["controller_trace"]), statistics.fmean(psi_sum(r) for r in t["controller_trace"] if "psi" in r), t["foreground"]["p95_ms"]) for t in obs if any("psi" in r for r in t["controller_trace"])]
    rho_wsi = spearman([a for a, _, _ in trial_pts], [c for _, _, c in trial_pts], permutations=2000) if len(trial_pts) >= 5 else (None, None)
    rho_psi = spearman([b for _, b, _ in trial_pts], [c for _, _, c in trial_pts], permutations=2000) if len(trial_pts) >= 5 else (None, None)
    truth = {"cpu": "cpu", "membw": "mem", "io": "io"}; attr = {}
    for preset, want in truth.items():
        hot = [r for p, r in periods if p == preset and r["wsi"] >= 5.0]
        attr[preset] = {"periods": len(hot), "correct": sum(r["attributed"] == want for r in hot), "accuracy": (sum(r["attributed"] == want for r in hot) / len(hot)) if hot else None}
    idle_fpr = sum(1 for v in idle_w if v >= 5.0) / len(idle_w) if idle_w else None
    p95 = {p: statistics.median([t["foreground"]["p95_ms"] for t in trials if t["preset"] == p and t["policy"] == "none" and not t["errors"]] or [float("nan")]) for p in PRESETS}
    p95_obs = {p: statistics.median([t["foreground"]["p95_ms"] for t in obs if t["preset"] == p] or [float("nan")]) for p in PRESETS}
    shares = [t["control_cpu_share"] for t in obs]
    return {"periods": len(periods), "trials": len(trials), "errors": sum(1 for t in trials if t["errors"]), "per_period_spearman_wsi_vs_psi": per_period[0],
            "per_resource_spearman": by_res, "detection": detect, "trial_spearman_p95": {"canary_wsi": rho_wsi[0], "canary_p": rho_wsi[1], "kernel_psi": rho_psi[0], "psi_p": rho_psi[1], "n": len(trial_pts)},
            "attribution": attr, "idle_false_engage": idle_fpr, "fg_p95_median_none": p95, "fg_p95_median_observe": p95_obs, "controller_cpu_share_median": statistics.median(shares) if shares else None}


def write_validity(s: dict[str, Any], out: Path) -> None:
    f = lambda v, d=2: "-" if v is None or v != v else f"{v:.{d}f}"
    L = ["# Linux cross-validation: canary WSI vs. kernel PSI", "", f"{s['trials']} trials ({s['errors']} with errors), {s['periods']} control periods in observe-only runs.", "",
         f"- Per-period Spearman, canary WSI vs. PSI some (cpu+io+mem): **{f(s['per_period_spearman_wsi_vs_psi'])}**",
         f"- Per-resource: canary CPU excess vs. PSI cpu {f(s['per_resource_spearman']['cpu'])}; canary storage excess vs. PSI io {f(s['per_resource_spearman']['io'])}",
         f"- Which predicts a separate app's p95 (per trial, n={s['trial_spearman_p95']['n']})? canary WSI rho **{f(s['trial_spearman_p95']['canary_wsi'])}** (p={f(s['trial_spearman_p95']['canary_p'], 4)}), kernel PSI rho **{f(s['trial_spearman_p95']['kernel_psi'])}** (p={f(s['trial_spearman_p95']['psi_p'], 4)})",
         f"- False engagement on idle periods: {f(100 * s['idle_false_engage'] if s['idle_false_engage'] is not None else None, 1)}%; control loop CPU: {f(100 * (s['controller_cpu_share_median'] or 0), 2)}% of one core", "",
         "| Workload | canary AUC vs idle | PSI AUC vs idle | median WSI % | median PSI some cpu/io/mem % | fg p95 (ms) | attribution |", "|---|---:|---:|---:|---|---:|---:|"]
    for p, d in s["detection"].items():
        a = s["attribution"].get(p, {}); acc = f"{a['correct']}/{a['periods']}" if a.get("periods") else "-"
        L.append(f"| {p} | {f(d['canary_auc'], 3)} | {f(d['psi_auc'], 3)} | {f(d['canary_wsi_median'], 1)} | {f(d['psi_some_median']['cpu'], 1)} / {f(d['psi_some_median']['io'], 1)} / {f(d['psi_some_median']['mem'], 1)} | {f(s['fg_p95_median_none'].get(p))} | {acc} |")
    sc = s.get("attribution_rule_vs_fingerprint") or {}
    if any(k in sc for k in ("cpu", "membw", "io")):
        L += ["", "## Attribution on the same stalled periods: ordered rule vs. learned fingerprints", "", "| Workload | stalled periods | ordered rule | fingerprints |", "|---|---:|---:|---:|"]
        L += [f"| {k} | {v['periods']} | {100 * v['rule_accuracy']:.1f}% | {100 * v['fingerprint_accuracy']:.1f}% |" for k, v in sc.items() if k in ("cpu", "membw", "io")]
        if "mixed_mean_shares" in sc: L.append(f"\nMixed workload, mean fingerprint shares: {sc['mixed_mean_shares']}")
    (out / "RESULTS.md").write_text("\n".join(L) + "\n", encoding="utf-8")


def analyse(study: Path, out: Path) -> Path:
    design = json.loads((study / "manifest.json").read_text())["design_name"]; out.mkdir(parents=True, exist_ok=True)
    if design == "validity":
        from research.designs import score_attribution
        s = analyse_validity(study); s["attribution_rule_vs_fingerprint"] = score_attribution(study); (out / "summary.json").write_text(json.dumps(s, indent=2, default=float)); write_validity(s, out)
    elif design == "blame":
        from research.designs import analyse_blame
        analyse_blame(study, out)
    else:
        from research.designs import COMPARISONS, analyse_policies
        COMPARISONS.setdefault("mitigation", [("nice", "none"), ("adaptive", "none"), ("adaptive", "nice")])
        analyse_policies("mitigation", study, out)
    return out / "RESULTS.md"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter); sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run"); r.add_argument("--design", choices=["validity", "mitigation", "blame"], required=True); r.add_argument("--blocks", type=int, default=8)
    r.add_argument("--seed", type=int, default=20261013); r.add_argument("--study-id"); r.add_argument("--out", type=Path, default=DATA / "results")
    r.add_argument("--fingerprint", action="store_true", help="learn contention fingerprints per block and attribute with them")
    a = sub.add_parser("analyze"); a.add_argument("study", type=Path); a.add_argument("--out", type=Path)
    args = ap.parse_args()
    if args.cmd == "run": study = run(args); print(analyse(study, study / "analysis"))
    else: print(analyse(args.study, args.out or args.study / "analysis"))


if __name__ == "__main__":
    main()
