"""Confounding robustness of blame confirmation, by simulation.

    python -m research.simulate_blame [--runs 300] [--out sim-blame]

The live-desktop risk of interventional blame is that the background load
changes *during* a test, so a suspect that did nothing looks guilty (or a
culprit looks innocent). This sweep drives the real Interrogator and
FactorialInterrogator classes against a toy desktop whose stall is
    WSI(t) = base(t) + effect * [culprit not nudged] + noise
where base(t) steps by `drift` WSI points at a random time during the first
test (load arriving or leaving, as when a sync client starts or a build
finishes). The innocent suspect is ranked first, the worst case for a
sequential test. For each drift we report the false-blame rate (the innocent
app confirmed) and the miss rate (the culprit never confirmed).

This is a simulation of the decision procedures only; it says nothing about
whether a real nudge helps (that is what the blame study measures).
"""
from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

from backend.blame import FactorialInterrogator, Interrogator, Suspect

CULPRIT, INNOCENT = 1, 2


def run_once(kind: str, drift: float, effect: float, noise: float, seed: int, periods: int = 90) -> tuple[bool, bool]:
    rng = random.Random(seed)
    it = (FactorialInterrogator(seed=seed, presume_guilty=True) if kind == "presumed" else FactorialInterrogator(seed=seed) if kind == "factorial" else Interrogator("interventional"))
    ranked = [Suspect(INNOCENT, "innocent", [INNOCENT], 0.9, 0.9, 0.0, 1.0, 1.0, False), Suspect(CULPRIT, "culprit", [CULPRIT], 0.5, 0.5, 0.0, 1.0, 1.0, False)]
    step_at = rng.randrange(2, 12); base0 = 6.0; history: list[float] = []; restored_at = None
    if kind == "presumed": periods = 300  # a 60 s episode: re-tests keep pooling evidence, so report time to restore
    for t in range(periods):
        base = base0 + (drift if t >= step_at else 0.0)
        wsi = max(0.0, base + (effect if CULPRIT not in it.nudged else 0.0) + rng.gauss(0, noise))
        history.append(wsi); it.step(True, "cpu", history, ranked)
        if kind == "presumed" and restored_at is None and any(v["app"] == INNOCENT and v["verdict"] == "exonerated" for v in it.verdicts): restored_at = t  # a verdict, not a test slot
    if kind == "presumed":  # verdicts are about release: harm = culprit set free, cost = time the innocent stays lowered
        last = {v["app"]: v["verdict"] for v in it.verdicts}  # end state by verdict, so a mid-experiment slot does not count
        return last.get(CULPRIT) == "exonerated", last.get(INNOCENT) != "exonerated", restored_at
    confirmed = {v["app"] for v in it.verdicts if v["verdict"] == "confirmed"}
    return INNOCENT in confirmed, CULPRIT not in confirmed


def sweep(runs: int, effect: float = 6.0, noise: float = 1.0) -> list[dict]:
    rows = []
    for drift in (-8.0, -6.0, -4.0, -2.0, 0.0, 2.0, 4.0):
        for kind in ("interventional", "factorial", "presumed"):
            outcomes = [run_once(kind, drift, effect, noise, seed) for seed in range(runs)]
            row = {"drift": drift, "method": kind, "runs": runs, "false_blame": sum(o[0] for o in outcomes) / runs, "miss": sum(o[1] for o in outcomes) / runs}
            if kind == "presumed":
                times = sorted(o[2] for o in outcomes if o[2] is not None)
                row["restore_median_s"] = round(times[len(times) // 2] * 0.2, 1) if times else None
            rows.append(row)
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs", type=int, default=300); ap.add_argument("--out", type=Path, default=Path("sim-blame"))
    args = ap.parse_args(); args.out.mkdir(parents=True, exist_ok=True)
    rows = sweep(args.runs)
    (args.out / "sweep.json").write_text(json.dumps(rows, indent=2))
    L = ["# Blame under load drift (simulation)", "", f"Culprit effect 6 WSI points, noise sd 1, {args.runs} runs per cell; the innocent app is ranked first.", "",
         "| Load drift during test (WSI points) | Method | False blame (innocent confirmed) | Miss (culprit never confirmed) |", "|---:|---|---:|---:|"]
    L += [f"| {r['drift']:+.0f} | {r['method']} | {100 * r['false_blame']:.1f}% | {100 * r['miss']:.1f}% |" for r in rows if r["method"] != "presumed"]
    L += ["", "Presumed guilty (the shipped mode): verdicts decide *release*, so the errors that matter differ.", "",
          "| Load drift | Culprit wrongly set free | Innocent still lowered after 60 s | Median time to restore the innocent |", "|---:|---:|---:|---:|"]
    L += [f"| {r['drift']:+.0f} | {100 * r['false_blame']:.1f}% | {100 * r['miss']:.1f}% | {r['restore_median_s']} s |" for r in rows if r["method"] == "presumed"]
    (args.out / "RESULTS.md").write_text("\n".join(L) + "\n")
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(figsize=(3.4, 2.2))
        for kind, color in (("interventional", "#eda100"), ("factorial", "#1baf7a")):
            pts = [r for r in rows if r["method"] == kind]
            ax.plot([r["drift"] for r in pts], [100 * r["false_blame"] for r in pts], marker="o", color=color, label={"interventional": "One at a time", "factorial": "Randomized factorial"}[kind])
        ax.set_xlabel("Load drift during the test (WSI points)", fontsize=7); ax.set_ylabel("Innocent app blamed (%)", fontsize=7); ax.tick_params(labelsize=6.5); ax.legend(fontsize=6.5)
        fig.tight_layout(); fig.savefig(args.out / "fig_blame_drift.pdf"); fig.savefig(args.out / "fig_blame_drift.png"); plt.close(fig)
    except ImportError: pass
    print(args.out / "RESULTS.md")


if __name__ == "__main__":
    main()
