"""Randomised complete-block study of interference-mitigation policies.

Every block runs each (workload preset, policy) cell once, in a fresh random
order drawn from a seeded RNG, after re-calibrating the canary on an idle host.
Blocking + randomisation spreads slow drift (thermals, turbo budget, background
OS activity) evenly across policies instead of letting it bias one of them.

Results stream to <out>/trials.jsonl (one JSON object per trial) so a study can
be interrupted and resumed with the same --study-id. The default output lives
under %LOCALAPPDATA% rather than the repository, because a synced folder
(OneDrive) re-uploads the file after every trial and perturbs the measurement;
copy the finished study into smartswap/research/results/ afterwards.

    python -m research.run_study --blocks 10
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import random
import subprocess
import sys
import time
from pathlib import Path

import psutil

from backend.controller import ControllerConfig
from backend.engine import CANARY_ENV, CONTROL_PERIOD_S, FG_ENV, POLICIES, PRESETS, STATIC_CAP_PERCENT, calibrate_canary, run_trial

ROOT = Path(__file__).resolve().parent
DEFAULT_OUT = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "SmartSwap" / "results"
MAIN_POLICIES = ["none", "observe", "nice", "static", "adaptive"]
ABLATION_POLICIES = ["adaptive_noladder", "adaptive_noaimd", "adaptive_nobenefit"]
ABLATION_PRESETS = ["cpu", "membw", "mixed"]


SWEEP_PRESETS = ["cpu", "membw", "mixed"]
SWEEP_POLICIES = ["none", "adaptive_slo1", "adaptive", "adaptive_slo6"]


HYBRID_PRESETS = ["cpu", "membw", "mixed"]
HYBRID_POLICIES = ["none", "nice", "ecore", "adaptive", "adaptive_hybrid"]


def cells(design: str = "main") -> list[tuple[str, str]]:
    if design == "slo-sweep":  # protection-vs-cost frontier of the v2 SLO knob
        return [(preset, policy) for preset in SWEEP_PRESETS for policy in SWEEP_POLICIES]
    if design == "hybrid":  # core-class steering: mechanism (ecore) and controller (adaptive_hybrid)
        return [(preset, policy) for preset in HYBRID_PRESETS for policy in HYBRID_POLICIES]
    main = [(preset, policy) for preset in PRESETS for policy in MAIN_POLICIES]
    return main + [(preset, policy) for preset in ABLATION_PRESETS for policy in ABLATION_POLICIES]


def host_manifest() -> dict:
    def sh(cmd: list[str]) -> str:
        try: return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout.strip()
        except Exception: return ""
    cpu_name = sh(["powershell", "-NoProfile", "-Command", "(Get-CimInstance Win32_Processor).Name"])
    battery = psutil.sensors_battery()
    return {
        "os": platform.platform(), "python": sys.version.split()[0], "cpu": cpu_name or platform.processor(),
        "logical_cpus": os.cpu_count(), "physical_cpus": psutil.cpu_count(logical=False),
        "ram_gb": round(psutil.virtual_memory().total / 2**30, 1),
        "on_ac_power": None if battery is None else battery.power_plugged,
        "power_plan": sh(["powercfg", "/getactivescheme"]),
        "git_commit": sh(["git", "-C", str(ROOT), "rev-parse", "HEAD"]),
    }


def done_cells(path: Path) -> set[tuple[int, str, str]]:
    if not path.exists(): return set()
    out = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line); out.add((row["block"], row["preset"], row["policy"]))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--blocks", type=int, default=10)
    ap.add_argument("--seed", type=int, default=20261006)
    ap.add_argument("--study-id", default=None)
    ap.add_argument("--settle", type=float, default=0.5, help="extra idle seconds between trials (plus a 1 s load sample)")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--design", choices=["main", "slo-sweep", "hybrid"], default="main")
    args = ap.parse_args()
    study_id = args.study_id or time.strftime("study-%Y%m%d-%H%M%S")
    out = args.out / study_id; out.mkdir(parents=True, exist_ok=True)
    trials_path = out / "trials.jsonl"; manifest_path = out / "manifest.json"
    if not manifest_path.exists():
        manifest_path.write_text(json.dumps({
            "study_id": study_id, "seed": args.seed, "blocks": args.blocks, "design": "randomised complete block",
            "design_name": args.design, "cells": cells(args.design), "presets": {k: v for k, v in PRESETS.items()}, "policies": POLICIES,
            "foreground": FG_ENV, "canary": CANARY_ENV, "control_period_s": CONTROL_PERIOD_S,
            "controller": ControllerConfig().__dict__, "static_cap_percent": STATIC_CAP_PERCENT,
            "host": host_manifest(), "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }, indent=2), encoding="utf-8")
    finished = done_cells(trials_path)
    rng = random.Random(args.seed)
    total = args.blocks * len(cells(args.design)); count = len(finished); t_start = time.time()
    for block in range(args.blocks):
        order = cells(args.design); rng.shuffle(order)  # drawn even for finished blocks so resumes keep the same order
        if all((block, p, q) in finished for p, q in order): continue
        time.sleep(3)
        cal = calibrate_canary()
        for position, (preset, policy) in enumerate(order):
            if (block, preset, policy) in finished: continue
            host_load = psutil.cpu_percent(interval=1.0)  # covariate: non-study load just before the trial
            result = run_trial(preset, policy, cal, seed=block * 100 + position)
            result.update(block=block, position=position, study_id=study_id, host_cpu_pct_before=host_load)
            with trials_path.open("a", encoding="utf-8") as handle: handle.write(json.dumps(result) + "\n")
            count += 1
            f = result["foreground"]; eta = (time.time() - t_start) / max(1, count - len(finished)) * (total - count)
            print(f"[{count}/{total}] block {block} {preset:5} {policy:18} p95={f.get('p95_ms')} bg={result['background_units_per_s']} acts={result['action_count']} err={len(result['errors'])} eta={eta/60:.0f}m", flush=True)
            time.sleep(args.settle)
    print(f"done: {trials_path}")


if __name__ == "__main__":
    main()
