"""Statistical analysis and figures for a SmartSwap block study.

    python -m research.analyze <study_dir> [--out <dir>]

Inference is paired by block (each block ran every cell once, randomised).
For each comparison we report the median of per-block ratios with a 95%
percentile-bootstrap CI (10 000 resamples over blocks), an exact two-sided
Wilcoxon signed-rank p-value on paired differences, and Holm-Bonferroni
adjustment across every comparison in the family. Nothing is averaged across
workload presets: each preset is its own experiment.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any

import numpy as np

from backend.engine import summarize_rows
from backend.stats import bootstrap_median_ci, holm, rank, spearman, wilcoxon_signed_rank

PRESET_ORDER = ["idle", "cpu", "membw", "io", "mixed"]
PRESET_NAME = {"idle": "No interference", "cpu": "CPU", "membw": "Memory bandwidth", "io": "Storage reads", "mixed": "Mixed"}
POLICY_ORDER = ["none", "observe", "nice", "static", "adaptive"]
POLICY_NAME = {"none": "None", "observe": "Observe-only", "nice": "Static priority", "static": "SmartSwap v1 (static cap)", "adaptive": "SmartSwap v2 (adaptive)",
               "adaptive_noladder": "v2 w/o ladder", "adaptive_noaimd": "v2 w/o AIMD", "adaptive_nobenefit": "v2 w/o benefit check"}
ABLATIONS = ["adaptive_noladder", "adaptive_noaimd", "adaptive_nobenefit"]
GROUND_TRUTH = {"cpu": "cpu", "membw": "mem", "io": "io"}
# Reference categorical palette (dataviz skill), fixed per policy; "none" is neutral ink.
COLOR = {"none": "#8a8984", "observe": "#4a3aa7", "nice": "#2a78d6", "static": "#eb6834", "adaptive": "#1baf7a",
         "adaptive_noladder": "#e87ba4", "adaptive_noaimd": "#eda100", "adaptive_nobenefit": "#e34948"}
MARKER = {"none": "o", "observe": "v", "nice": "s", "static": "^", "adaptive": "D", "adaptive_noladder": "P", "adaptive_noaimd": "X", "adaptive_nobenefit": "*"}
RNG = np.random.default_rng(7)


def load(study: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    manifest = json.loads((study / "manifest.json").read_text(encoding="utf-8"))
    trials = [json.loads(l) for l in (study / "trials.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    # Recompute every summary from the raw beats so all quantiles use the same
    # (nearest-rank) definition, independent of the engine version that ran the trial.
    interval_ms = float(manifest["foreground"]["SS_INTERVAL"]) * 1000
    for t in trials:
        rows = t.get("foreground_rows") or []
        if rows: t["foreground"] = summarize_rows(rows, interval_ms, len(rows) * interval_ms + t["foreground"].get("skipped_beats", 0) * interval_ms)
    return manifest, trials


def throughput(trial: dict[str, Any]) -> dict[str, float]:
    return trial.get("background_units_per_s") or {}


def retained(trial: dict[str, Any], ref: dict[str, Any]) -> float | None:
    """Background work retained vs the no-mitigation run of the same block (roles weighted equally)."""
    a, b = throughput(trial), throughput(ref)
    ratios = [a.get(role, 0.0) / b[role] for role in b if b[role] > 0]
    return statistics.fmean(ratios) if ratios else None


boot_median_ci = bootstrap_median_ci
wilcoxon_p = wilcoxon_signed_rank


def index(trials: list[dict[str, Any]]) -> dict[tuple[str, str], dict[int, dict[str, Any]]]:
    out: dict[tuple[str, str], dict[int, dict[str, Any]]] = defaultdict(dict)
    for t in trials:
        if not t["errors"] and t["foreground"].get("beats"): out[(t["preset"], t["policy"])][t["block"]] = t
    return out


def mann_whitney_auc(pos: list[float], neg: list[float]) -> float:
    """P(score of a positive > score of a negative), ties counted half: the ROC AUC."""
    if not pos or not neg: return float("nan")
    ranks = rank(pos + neg); r_pos = sum(ranks[: len(pos)])
    return (r_pos - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg))


def wsi_validity(trials: list[dict[str, Any]], engage: float) -> dict[str, Any]:
    """Is the canary's WSI a proxy for what the (separate) foreground experiences?

    Uses observe-only trials: the controller computes WSI but never acts, so the
    foreground's p95 is not influenced by anything the WSI caused.
    """
    obs = [t for t in trials if t["policy"] == "observe" and t.get("controller_trace") and t["foreground"].get("p95_ms")]
    pts = [(statistics.fmean(r["wsi"] for r in t["controller_trace"]), t["foreground"]["p95_ms"], t["preset"]) for t in obs]
    rho, p = spearman([a for a, _, _ in pts], [b for _, b, _ in pts]) if len(pts) >= 5 else (float("nan"), float("nan"))
    roc = {}
    idle_periods = [r["wsi"] for t in obs if t["preset"] == "idle" for r in t["controller_trace"]]
    for preset in ("cpu", "membw", "io", "mixed"):
        pos = [r["wsi"] for t in obs if t["preset"] == preset for r in t["controller_trace"]]
        curve = []
        for th in [0.5, 1, 2, 3, 4, 5, 6, 8, 10, 15, 20, 30, 50]:
            curve.append({"threshold": th, "tpr": sum(v >= th for v in pos) / len(pos) if pos else None, "fpr": sum(v >= th for v in idle_periods) / len(idle_periods) if idle_periods else None})
        roc[preset] = {"auc": mann_whitney_auc(pos, idle_periods), "periods": len(pos),
                       "tpr_at_engage": sum(v >= engage for v in pos) / len(pos) if pos else None, "curve": curve}
    fpr_engage = sum(v >= engage for v in idle_periods) / len(idle_periods) if idle_periods else None
    return {"n_trials": len(pts), "spearman_rho": rho, "spearman_p": p, "points": pts, "roc": roc, "idle_periods": len(idle_periods), "fpr_at_engage": fpr_engage}


def compare(idx, preset: str, a: str, b: str, metric: str = "p95_ms") -> dict[str, Any] | None:
    """Policy a relative to policy b, paired by block."""
    blocks = sorted(set(idx[(preset, a)]) & set(idx[(preset, b)]))
    if len(blocks) < 3: return None
    va = [idx[(preset, a)][k]["foreground"][metric] for k in blocks]; vb = [idx[(preset, b)][k]["foreground"][metric] for k in blocks]
    ratios = [x / y for x, y in zip(va, vb)]
    lo, hi = boot_median_ci(ratios)
    ret = [retained(idx[(preset, a)][k], idx[(preset, "none")][k]) for k in blocks if k in idx[(preset, "none")]]
    ret = [r for r in ret if r is not None]
    rlo, rhi = boot_median_ci(ret) if len(ret) >= 2 else (float("nan"), float("nan"))
    return {"preset": preset, "policy": a, "reference": b, "metric": metric, "n": len(blocks),
            "median_a": statistics.median(va), "median_b": statistics.median(vb),
            "median_ratio": statistics.median(ratios), "ci_low": lo, "ci_high": hi, "p": wilcoxon_p([x - y for x, y in zip(va, vb)]),
            "bg_retained_median": statistics.median(ret) if ret else None, "bg_retained_ci": [rlo, rhi]}


def attribution_matrix(trials: list[dict[str, Any]], engage_wsi: float) -> dict[str, dict[str, int]]:
    """Per-period attribution in observe-only runs (actions disabled, so unconfounded)."""
    m: dict[str, dict[str, int]] = {p: defaultdict(int) for p in PRESET_ORDER}
    for t in trials:
        if t["policy"] != "observe": continue
        for row in t.get("controller_trace", []):
            label = row["attributed"] if row["wsi"] >= engage_wsi else "below threshold"
            m[t["preset"]][label] += 1
    return {p: dict(v) for p, v in m.items()}


def fmt_ratio(c: dict[str, Any]) -> str:
    return f"{c['median_ratio']:.2f} [{c['ci_low']:.2f}, {c['ci_high']:.2f}]"


def analyse(study: Path, out: Path) -> dict[str, Any]:
    manifest, trials = load(study)
    out.mkdir(parents=True, exist_ok=True); (out / "figures").mkdir(exist_ok=True); (out / "tables").mkdir(exist_ok=True)
    idx = index(trials)
    cfg = manifest["controller"]
    errors = [t for t in trials if t["errors"]]

    # 1. Validity: does each workload actually interfere? (none vs idle/none)
    validity = {}
    for preset in PRESET_ORDER[1:]:
        blocks = sorted(set(idx[(preset, "none")]) & set(idx[("idle", "none")]))
        loaded = [idx[(preset, "none")][b]["foreground"]["p95_ms"] for b in blocks]; idle = [idx[("idle", "none")][b]["foreground"]["p95_ms"] for b in blocks]
        validity[preset] = {"n": len(blocks), "idle_p95_median": statistics.median(idle), "loaded_p95_median": statistics.median(loaded),
                            "inflation": statistics.median([a / b for a, b in zip(loaded, idle)]), "p": wilcoxon_p([a - b for a, b in zip(loaded, idle)])}

    # 2. Main comparisons, Holm-adjusted as one family.
    family = []
    for preset in PRESET_ORDER:
        for pol in ("nice", "static", "adaptive"):
            c = compare(idx, preset, pol, "none")
            if c: family.append(c)
        for ref in ("static", "nice"):
            c = compare(idx, preset, "adaptive", ref)
            if c: family.append(c)
    for preset in sorted({p for p, q in idx if q in ABLATIONS}):
        for pol in ABLATIONS:
            c = compare(idx, preset, pol, "adaptive")
            if c: family.append(c)
    overhead = [c for c in (compare(idx, "idle", "observe", "none", m) for m in ("p50_ms", "p95_ms", "p99_ms")) if c]
    family += overhead
    for c, p_adj in zip(family, holm([c["p"] for c in family])): c["p_holm"] = p_adj

    # 3. Per-cell descriptive summary.
    cells = {}
    for (preset, policy), by_block in idx.items():
        f = [t["foreground"] for t in by_block.values()]
        ret = [retained(t, idx[(preset, "none")][b]) for b, t in by_block.items() if b in idx[(preset, "none")]]
        ret = [r for r in ret if r is not None]
        cells[f"{preset}/{policy}"] = {
            "n": len(f), "p50_ms": statistics.median(x["p50_ms"] for x in f), "p95_ms": statistics.median(x["p95_ms"] for x in f),
            "p99_ms": statistics.median(x["p99_ms"] for x in f), "deadline_misses_mean": statistics.fmean(x["deadline_misses"] for x in f),
            "bg_retained_median": statistics.median(ret) if ret else None,
            "actions_median": statistics.median(t["action_count"] for t in by_block.values()),
            "actions_verified_share": (sum(t["actions_verified"] for t in by_block.values()) / max(1, sum(t["action_count"] for t in by_block.values()))),
            "reverts_verified_all": all(t["reverts_verified"] in (None, True) for t in by_block.values()),
            "time_to_first_action_s_median": statistics.median([t["time_to_first_action_s"] for t in by_block.values() if t["time_to_first_action_s"] is not None] or [float("nan")]),
            "engaged_share_median": statistics.median([t["controller_engaged_share"] for t in by_block.values() if t["controller_engaged_share"] is not None] or [float("nan")]),
        }

    attribution = attribution_matrix(trials, cfg["engage_wsi"])
    accuracy = {}
    for preset, truth in GROUND_TRUTH.items():
        row = attribution[preset]; engaged = sum(v for k, v in row.items() if k != "below threshold")
        accuracy[preset] = {"periods_above_threshold": engaged, "correct": row.get(truth, 0), "accuracy": row.get(truth, 0) / engaged if engaged else None}
    idle_row = attribution["idle"]; idle_total = sum(idle_row.values())
    false_engage_rate = (idle_total - idle_row.get("below threshold", 0)) / idle_total if idle_total else None

    host_load = [t.get("host_cpu_pct_before") for t in trials if t.get("host_cpu_pct_before") is not None]
    # Robustness: does pre-trial host load explain unmitigated p95 within a preset? (Spearman)
    load_corr = {}
    for preset in PRESET_ORDER:
        pts = [(t.get("host_cpu_pct_before"), t["foreground"]["p95_ms"]) for t in idx[(preset, "none")].values() if t.get("host_cpu_pct_before") is not None]
        if len(pts) >= 5:
            rho, p = spearman([a for a, _ in pts], [b for _, b in pts]); load_corr[preset] = {"rho": rho, "p": p, "n": len(pts)}

    validity_wsi = wsi_validity(trials, cfg["engage_wsi"])
    all_actions = [e for t in trials for e in t["actions"] if not e["kind"].endswith("_revert")]
    all_reverts = [e for t in trials for e in t["actions"] if e["kind"].endswith("_revert")]
    summary = {
        "study_id": manifest["study_id"], "host": manifest["host"], "trials": len(trials), "trials_with_errors": len(errors),
        "error_examples": [t["errors"][:2] for t in errors[:5]], "blocks": manifest["blocks"],
        "validity": validity, "comparisons": family, "cells": cells,
        "attribution_counts": attribution, "attribution_accuracy": accuracy, "idle_false_engage_rate": false_engage_rate,
        "host_cpu_pct_before": {"median": statistics.median(host_load), "p90": float(np.percentile(host_load, 90)), "max": max(host_load)} if host_load else None,
        "host_load_vs_p95_spearman": load_corr,
        "wsi_validity": {k: v for k, v in validity_wsi.items() if k != "points"},
        "ledger": {"actions": len(all_actions), "actions_verified": sum(e["verified"] for e in all_actions), "reverts": len(all_reverts), "reverts_verified": sum(e["verified"] for e in all_reverts),
                   "cap_rollbacks": sum(1 for t in trials for e in t["actions"] if e["kind"] == "cpu_rate_cap_revert" and "benefit check failed" in e["reason"])},
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2, default=float), encoding="utf-8")
    write_tables(summary, out / "tables")
    make_figures(trials, idx, summary, manifest, out / "figures")
    make_validity_figure(validity_wsi, manifest, out / "figures")
    write_markdown(summary, out / "RESULTS.md")
    return summary


def _p(p: float) -> str:
    return "<0.001" if p < 0.001 else f"{p:.3f}"


def write_tables(s: dict[str, Any], out: Path) -> None:
    comps = {(c["preset"], c["policy"], c["reference"], c["metric"]): c for c in s["comparisons"]}
    lines = [r"\begin{tabular}{llrrrr}", r"\toprule", r"Workload & Policy & p95 (ms) & p95 ratio vs.\ none [95\% CI] & $p_{\mathrm{Holm}}$ & BG work kept \\", r"\midrule"]
    for preset in PRESET_ORDER:
        first = True
        for pol in ("none", "nice", "static", "adaptive"):
            cell = s["cells"].get(f"{preset}/{pol}")
            if not cell: continue
            c = comps.get((preset, pol, "none", "p95_ms"))
            ratio = "--" if pol == "none" else fmt_ratio(c) if c else "--"
            p = "--" if pol == "none" or not c else _p(c["p_holm"])
            kept = "100\\%" if pol == "none" else (f"{100 * cell['bg_retained_median']:.0f}\\%" if cell["bg_retained_median"] is not None else "--")
            lines.append(f"{PRESET_NAME[preset] if first else ''} & {POLICY_NAME[pol]} & {cell['p95_ms']:.2f} & {ratio} & {p} & {kept} \\\\"); first = False
        lines.append(r"\midrule" if preset != PRESET_ORDER[-1] else r"\bottomrule")
    lines.append(r"\end{tabular}")
    (out / "main_results.tex").write_text("\n".join(lines), encoding="utf-8")

    lines = [r"\begin{tabular}{llrrr}", r"\toprule", r"Workload & Variant & p95 ratio vs.\ full v2 [95\% CI] & $p_{\mathrm{Holm}}$ & BG work kept \\", r"\midrule"]
    for preset in ("cpu", "membw", "mixed"):
        first = True
        for pol in ABLATIONS:
            c = comps.get((preset, pol, "adaptive", "p95_ms")); cell = s["cells"].get(f"{preset}/{pol}")
            if not c or not cell: continue
            kept = f"{100 * cell['bg_retained_median']:.0f}\\%" if cell["bg_retained_median"] is not None else "--"
            lines.append(f"{PRESET_NAME[preset] if first else ''} & {POLICY_NAME[pol]} & {fmt_ratio(c)} & {_p(c['p_holm'])} & {kept} \\\\"); first = False
    lines += [r"\bottomrule", r"\end{tabular}"]
    (out / "ablation.tex").write_text("\n".join(lines), encoding="utf-8")

    lines = [r"\begin{tabular}{lrrr}", r"\toprule", r"Workload & Periods $\geq$ engage threshold & Correctly attributed & Accuracy \\", r"\midrule"]
    for preset, a in s["attribution_accuracy"].items():
        acc = f"{100 * a['accuracy']:.1f}\\%" if a["accuracy"] is not None else "--"
        lines.append(f"{PRESET_NAME[preset]} & {a['periods_above_threshold']} & {a['correct']} & {acc} \\\\")
    fe = s["idle_false_engage_rate"]
    lines += [r"\midrule", f"No interference & \\multicolumn{{3}}{{r}}{{periods above threshold: {100 * fe:.1f}\\%}} \\\\" if fe is not None else "", r"\bottomrule", r"\end{tabular}"]
    (out / "attribution.tex").write_text("\n".join(lines), encoding="utf-8")


def make_figures(trials, idx, s, manifest, out: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update({"font.family": "serif", "font.size": 8, "axes.spines.top": False, "axes.spines.right": False, "axes.edgecolor": "#52514e",
                         "axes.labelcolor": "#0b0b0b", "xtick.color": "#52514e", "ytick.color": "#52514e", "axes.grid": True, "grid.color": "#e4e3df", "grid.linewidth": 0.5,
                         "legend.frameon": False, "savefig.bbox": "tight", "savefig.dpi": 300, "pdf.fonttype": 42})

    def save(fig, name):
        fig.savefig(out / f"{name}.pdf"); fig.savefig(out / f"{name}.png"); plt.close(fig)

    # Fig. 1: p95 per workload x policy, one dot per block, median bar.
    pols = ["none", "nice", "static", "adaptive"]
    fig, axes = plt.subplots(1, len(PRESET_ORDER), figsize=(7.0, 2.1), sharey=False)
    for ax, preset in zip(axes, PRESET_ORDER):
        for i, pol in enumerate(pols):
            vals = [t["foreground"]["p95_ms"] for t in idx[(preset, pol)].values()]
            if not vals: continue
            x = i + (RNG.random(len(vals)) - 0.5) * 0.35
            ax.scatter(x, vals, s=9, color=COLOR[pol], marker=MARKER[pol], alpha=0.75, linewidths=0, zorder=3)
            ax.hlines(statistics.median(vals), i - 0.3, i + 0.3, color="#0b0b0b", linewidth=1.2, zorder=4)
        ax.set_title(PRESET_NAME[preset], fontsize=8); ax.set_xticks(range(len(pols))); ax.set_xticklabels(["None", "Prio", "v1", "v2"], fontsize=7)
        ax.set_ylim(bottom=0); ax.grid(axis="x", visible=False)
    axes[0].set_ylabel("Foreground p95 latency (ms)")
    save(fig, "fig_p95_by_policy")

    # Fig. 2: protection vs cost frontier per workload.
    fig, axes = plt.subplots(1, 4, figsize=(7.0, 2.5), sharey=True)
    for ax, preset in zip(axes, ["cpu", "membw", "io", "mixed"]):
        for pol in ["none", "nice", "static", "adaptive"] + ([p for p in ABLATIONS if idx.get((preset, p))]):
            blocks = sorted(set(idx[(preset, pol)]) & set(idx[(preset, "none")]))
            if not blocks: continue
            kept = [retained(idx[(preset, pol)][b], idx[(preset, "none")][b]) for b in blocks]
            red = [1 - idx[(preset, pol)][b]["foreground"]["p95_ms"] / idx[(preset, "none")][b]["foreground"]["p95_ms"] for b in blocks]
            kx, ky = statistics.median(kept) * 100, statistics.median(red) * 100
            xl, xh = boot_median_ci([k * 100 for k in kept]); yl, yh = boot_median_ci([r * 100 for r in red])
            ax.errorbar(kx, ky, xerr=[[max(0, kx - xl)], [max(0, xh - kx)]] if not math.isnan(xl) else None, yerr=[[max(0, ky - yl)], [max(0, yh - ky)]] if not math.isnan(yl) else None,
                        fmt=MARKER[pol], color=COLOR[pol], ms=5 if pol in POLICY_ORDER else 4, elinewidth=0.7, capsize=0, label=POLICY_NAME[pol], zorder=3)
        ax.set_title(PRESET_NAME[preset], fontsize=8); ax.set_xlim(0, 110)
    axes[0].set_ylabel("p95 reduction vs. none (%)"); fig.supxlabel("Background work kept vs. none (%)  -  up and right is better", fontsize=8, y=0.02)
    handles, labels = [], []
    for ax in axes:
        for h, l in zip(*ax.get_legend_handles_labels()):
            if l not in labels: handles.append(h); labels.append(l)
    fig.subplots_adjust(top=0.70, bottom=0.2, wspace=0.12)
    fig.legend(handles, labels, loc="upper center", ncol=4, bbox_to_anchor=(0.5, 1.0), fontsize=7)
    save(fig, "fig_tradeoff")

    # Fig. 3: latency CDF under CPU and memory contention (all beats pooled).
    fig, axes = plt.subplots(1, 2, figsize=(7.0, 2.0))
    for ax, preset in zip(axes, ["cpu", "membw"]):
        for pol in ["none", "nice", "static", "adaptive"]:
            lat = sorted(r["sched"] + r["cpu"] + r["mem"] + r["io"] for t in idx[(preset, pol)].values() for r in t["foreground_rows"])
            if not lat: continue
            y = np.arange(1, len(lat) + 1) / len(lat)
            ax.plot(lat, y, color=COLOR[pol], linewidth=1.4, label=POLICY_NAME[pol])
        ax.set_xscale("log"); ax.set_title(PRESET_NAME[preset], fontsize=8); ax.set_xlabel("Foreground beat latency (ms, log)"); ax.set_ylim(0.5, 1.001)
    axes[0].set_ylabel("CDF (upper half)"); axes[1].legend(loc="lower right", fontsize=6.5)
    save(fig, "fig_latency_cdf")

    # Fig. 4: controller timeline for one adaptive trial per workload (median-p95 block).
    fig, axes = plt.subplots(2, 4, figsize=(7.0, 2.8), sharex=True, gridspec_kw={"height_ratios": [2, 1]})
    for col, preset in enumerate(["cpu", "membw", "io", "mixed"]):
        runs = sorted(idx[(preset, "adaptive")].values(), key=lambda t: t["foreground"]["p95_ms"])
        if not runs: continue
        t = runs[len(runs) // 2]; tr = t["controller_trace"]
        if not tr: continue
        t0 = tr[0]["t_ms"]; xs = [(r["t_ms"] - t0) / 1000 for r in tr]
        axes[0, col].plot(xs, [max(0.1, r["wsi"]) for r in tr], color="#2a78d6", linewidth=1.0)
        axes[0, col].axhline(manifest["controller"]["engage_wsi"], color="#8a8984", linewidth=0.6, linestyle="--")
        axes[0, col].axhline(manifest["controller"]["slo_wsi"], color="#8a8984", linewidth=0.6, linestyle=":")
        axes[0, col].set_yscale("log"); axes[0, col].set_ylim(0.1, 120); axes[0, col].set_title(PRESET_NAME[preset], fontsize=8)
        for ax in axes[:, col]:  # shade periods where the controller is engaged (hint and/or cap active)
            for x0, x1, r in zip(xs, xs[1:], tr):
                if r["engaged"]: ax.axvspan(x0, x1, color="#1baf7a", alpha=0.10, linewidth=0)
        axes[1, col].step(xs, [r["cap"] if r["cap"] is not None else 100 for r in tr], where="post", color="#eb6834", linewidth=1.0)
        axes[1, col].set_ylim(0, 108); axes[1, col].set_xlabel("Time (s)")
    axes[0, 0].set_ylabel("WSI (%, log)"); axes[1, 0].set_ylabel("CPU cap (%)")
    axes[0, 0].text(0.98, 0.95, "engage", transform=axes[0, 0].transAxes, ha="right", va="top", fontsize=6, color="#52514e")
    save(fig, "fig_controller_timeline")

    # Fig. 5: attribution confusion (observe-only).
    labels = ["cpu", "mem", "io", "none", "below threshold"]
    rows = [p for p in PRESET_ORDER]
    mat = np.array([[s["attribution_counts"][p].get(l, 0) for l in labels] for p in rows], dtype=float)
    share = mat / np.maximum(1, mat.sum(axis=1, keepdims=True))
    fig, ax = plt.subplots(figsize=(3.4, 2.0))
    ax.imshow(share, cmap="Blues", vmin=0, vmax=1, aspect="auto")
    for i in range(len(rows)):
        for j in range(len(labels)):
            ax.text(j, i, f"{100 * share[i, j]:.0f}", ha="center", va="center", fontsize=6.5, color="#ffffff" if share[i, j] > 0.55 else "#0b0b0b")
    ax.set_xticks(range(len(labels))); ax.set_xticklabels(["CPU", "Mem", "I/O", "None", "Below\nthresh."], fontsize=7)
    ax.set_yticks(range(len(rows))); ax.set_yticklabels([PRESET_NAME[p] for p in rows], fontsize=7); ax.grid(False)
    ax.set_xlabel("Attributed resource (% of control periods)")
    save(fig, "fig_attribution")


def make_validity_figure(v: dict[str, Any], manifest: dict[str, Any], out: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    tone = {"idle": "#8a8984", "cpu": "#2a78d6", "membw": "#eb6834", "io": "#1baf7a", "mixed": "#4a3aa7"}
    fig, axes = plt.subplots(1, 2, figsize=(7.0, 2.3))
    ax = axes[0]
    for preset in PRESET_ORDER:
        pts = [(a, b) for a, b, p in v["points"] if p == preset]
        if pts: ax.scatter([max(0.05, a) for a, _ in pts], [b for _, b in pts], s=14, color=tone[preset], marker=MARKER.get({"idle": "none", "cpu": "nice", "membw": "static", "io": "adaptive", "mixed": "observe"}[preset], "o"), label=PRESET_NAME[preset], linewidths=0, alpha=.85)
    ax.set_xscale("log"); ax.set_yscale("log"); ax.set_xlabel("Canary WSI, trial mean (%, log)"); ax.set_ylabel("Foreground p95 (ms, log)")
    ax.set_title(f"WSI vs. foreground p95 (Spearman $\\rho$={v['spearman_rho']:.2f})", fontsize=8); ax.legend(fontsize=6, loc="upper left")
    ax = axes[1]
    for preset in ("cpu", "membw", "io", "mixed"):
        r = v["roc"][preset]; c = [pt for pt in r["curve"] if pt["tpr"] is not None]
        ax.plot([0] + [pt["fpr"] for pt in c][::-1] + [1], [0] + [pt["tpr"] for pt in c][::-1] + [1], color=tone[preset], linewidth=1.4, label=f"{PRESET_NAME[preset]} (AUC {r['auc']:.2f})")
    ax.plot([0, 1], [0, 1], color="#cfcdc7", linewidth=0.8, linestyle=":")
    ax.set_xlabel("False-positive rate (no-interference periods)"); ax.set_ylabel("True-positive rate"); ax.set_title("Detecting interference from WSI", fontsize=8); ax.legend(fontsize=6, loc="lower right")
    fig.tight_layout(); fig.savefig(out / "fig_wsi_validity.pdf"); fig.savefig(out / "fig_wsi_validity.png"); plt.close(fig)


def analyse_sweep(study: Path, out: Path) -> dict[str, Any]:
    """SLO sweep: the protection-vs-cost frontier traced by v2's single knob."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    manifest, trials = load(study); idx = index(trials); out.mkdir(parents=True, exist_ok=True)
    slo = {"adaptive_slo1": 1.5, "adaptive": 3.0, "adaptive_slo6": 6.0}
    rows = []
    for preset in ("cpu", "membw", "mixed"):
        for pol, target in slo.items():
            c = compare(idx, preset, pol, "none")
            if c: rows.append({**c, "slo_wsi": target})
    for r, padj in zip(rows, holm([r["p"] for r in rows])): r["p_holm"] = padj
    fig, axes = plt.subplots(1, 3, figsize=(7.0, 2.1), sharey=True)
    for ax, preset in zip(axes, ("cpu", "membw", "mixed")):
        pts = [r for r in rows if r["preset"] == preset]
        xs = [100 * r["bg_retained_median"] for r in pts]; ys = [100 * (1 - r["median_ratio"]) for r in pts]
        ax.plot(xs, ys, color="#8a8984", linewidth=0.8, zorder=1)
        for r, x, y in zip(pts, xs, ys):
            ax.scatter([x], [y], s=34, color={1.5: "#4a3aa7", 3.0: "#1baf7a", 6.0: "#eda100"}[r["slo_wsi"]], zorder=2, label=f"SLO {r['slo_wsi']:g}% WSI")
        ax.set_title(PRESET_NAME[preset], fontsize=8); ax.set_xlim(0, 105)
    axes[0].set_ylabel("p95 reduction vs. none (%)"); fig.supxlabel("Background work kept (%)", fontsize=8, y=0.02)
    h, l = axes[0].get_legend_handles_labels(); fig.legend(h, l, loc="upper center", ncol=3, bbox_to_anchor=(0.5, 1.08), fontsize=7)
    fig.subplots_adjust(bottom=0.22, top=0.78); fig.savefig(out / "fig_slo_frontier.pdf"); fig.savefig(out / "fig_slo_frontier.png"); plt.close(fig)
    lines = [r"\begin{tabular}{lrrrr}", r"\toprule", r"Workload & SLO (WSI) & p95 ratio vs.\ none [95\% CI] & $p_{\mathrm{Holm}}$ & BG work kept \\", r"\midrule"]
    for r in rows: lines.append(f"{PRESET_NAME[r['preset']]} & {r['slo_wsi']:g}\\% & {fmt_ratio(r)} & {_p(r['p_holm'])} & {100 * r['bg_retained_median']:.0f}\\% \\\\")
    lines += [r"\bottomrule", r"\end{tabular}"]
    (out / "slo_sweep.tex").write_text("\n".join(lines), encoding="utf-8")
    summary = {"study_id": manifest["study_id"], "trials": len(trials), "rows": rows}
    (out / "sweep_summary.json").write_text(json.dumps(summary, indent=2, default=float), encoding="utf-8")
    return summary


def core_split(trial: dict[str, Any]) -> tuple[float, float] | None:
    """Mean busy % of P-cores and of E-cores during the foreground run."""
    busy = trial.get("percpu_busy") or []; classes = trial.get("core_classes") or {}
    if not busy or not classes: return None
    top = max(c["efficiency"] for c in classes.values())
    p = [busy[int(k)] for k, c in classes.items() if c["efficiency"] == top and int(k) < len(busy)]
    e = [busy[int(k)] for k, c in classes.items() if c["efficiency"] != top and int(k) < len(busy)]
    return (statistics.fmean(p), statistics.fmean(e)) if p and e else None


def analyse_hybrid(study: Path, out: Path) -> dict[str, Any]:
    """Core-class steering on a hybrid CPU: mechanism (where load lands) and benefit."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    manifest, trials = load(study); idx = index(trials); out.mkdir(parents=True, exist_ok=True)
    presets = ["cpu", "membw", "mixed"]; pols = ["none", "nice", "ecore", "adaptive", "adaptive_hybrid"]
    names = {**POLICY_NAME, "ecore": "Static E-core steering", "adaptive_hybrid": "SmartSwap v2 + steering"}
    family = []
    for preset in presets:
        for pol in pols[1:]:
            c = compare(idx, preset, pol, "none")
            if c: family.append(c)
        for a, b in (("adaptive_hybrid", "adaptive"), ("ecore", "nice"), ("adaptive_hybrid", "nice")):
            c = compare(idx, preset, a, b)
            if c: family.append(c)
    for c, padj in zip(family, holm([c["p"] for c in family])): c["p_holm"] = padj
    split = {}
    for preset in presets:
        for pol in pols:
            vals = [core_split(t) for t in idx[(preset, pol)].values()]; vals = [v for v in vals if v]
            if vals: split[f"{preset}/{pol}"] = {"p_core_busy": statistics.median(v[0] for v in vals), "e_core_busy": statistics.median(v[1] for v in vals), "n": len(vals)}
    steer_events = [e for t in trials if t["policy"] == "adaptive_hybrid" for e in t["actions"] if e["kind"] in ("steer_ecores", "steer_revert")]
    rollbacks = sum(1 for e in steer_events if e["kind"] == "steer_revert" and "benefit check failed" in e["reason"])
    summary = {"study_id": manifest["study_id"], "trials": len(trials), "errors": sum(1 for t in trials if t["errors"]), "comparisons": family, "core_split": split,
               "steer_actions": sum(1 for e in steer_events if e["kind"] == "steer_ecores"), "steer_verified": sum(1 for e in steer_events if e["kind"] == "steer_ecores" and e["verified"]), "steer_rollbacks": rollbacks,
               "cells": {f"{p_}/{q}": {"p95_ms": statistics.median(t["foreground"]["p95_ms"] for t in idx[(p_, q)].values()), "n": len(idx[(p_, q)])} for p_ in presets for q in pols if idx[(p_, q)]}}
    (out / "hybrid_summary.json").write_text(json.dumps(summary, indent=2, default=float), encoding="utf-8")

    tone = {"none": "#8a8984", "nice": "#2a78d6", "ecore": "#eda100", "adaptive": "#1baf7a", "adaptive_hybrid": "#4a3aa7"}
    fig, axes = plt.subplots(1, 3, figsize=(7.0, 2.2), sharey=True)
    for ax, preset in zip(axes, presets):
        xs = range(len(pols))
        pb = [split.get(f"{preset}/{q}", {}).get("p_core_busy", 0) for q in pols]; eb = [split.get(f"{preset}/{q}", {}).get("e_core_busy", 0) for q in pols]
        ax.bar([x - 0.2 for x in xs], pb, width=0.38, color="#2a78d6", label="P-cores busy")
        ax.bar([x + 0.2 for x in xs], eb, width=0.38, color="#eb6834", label="E-cores busy")
        ax.set_xticks(list(xs)); ax.set_xticklabels(["None", "Prio", "E-core", "v2", "v2+st"], fontsize=6.5); ax.set_title(PRESET_NAME[preset], fontsize=8); ax.set_ylim(0, 105); ax.grid(axis="x", visible=False)
    axes[0].set_ylabel("Mean core busy (%)"); axes[0].legend(fontsize=6.5, loc="upper right")
    fig.tight_layout(); fig.savefig(out / "fig_core_split.pdf"); fig.savefig(out / "fig_core_split.png"); plt.close(fig)

    fig, axes = plt.subplots(1, 3, figsize=(7.0, 2.2), sharey=True)
    for ax, preset in zip(axes, presets):
        for pol in pols:
            blocks = sorted(set(idx[(preset, pol)]) & set(idx[(preset, "none")]))
            if not blocks: continue
            kept = [100 * retained(idx[(preset, pol)][b], idx[(preset, "none")][b]) for b in blocks]
            red = [100 * (1 - idx[(preset, pol)][b]["foreground"]["p95_ms"] / idx[(preset, "none")][b]["foreground"]["p95_ms"]) for b in blocks]
            ax.scatter([statistics.median(kept)], [statistics.median(red)], s=40, color=tone[pol], marker=MARKER.get(pol, "o") if pol in MARKER else "h", label=names[pol], zorder=3)
        ax.set_title(PRESET_NAME[preset], fontsize=8); ax.set_xlim(0, 110)
    axes[0].set_ylabel("p95 reduction vs. none (%)"); fig.supxlabel("Background work kept (%)", fontsize=8, y=0.02)
    h, l = axes[0].get_legend_handles_labels(); fig.legend(h, l, loc="upper center", ncol=5, bbox_to_anchor=(0.5, 1.06), fontsize=6.5)
    fig.subplots_adjust(bottom=0.22, top=0.80, wspace=0.1); fig.savefig(out / "fig_hybrid_tradeoff.pdf"); fig.savefig(out / "fig_hybrid_tradeoff.png"); plt.close(fig)

    comps = {(c["preset"], c["policy"], c["reference"]): c for c in family}
    lines = [r"\begin{tabular}{llrrrr}", r"\toprule", r"Workload & Policy & p95 ratio vs.\ none [95\% CI] & $p_{\mathrm{Holm}}$ & BG kept & P/E busy \% \\", r"\midrule"]
    for preset in presets:
        first = True
        for pol in pols[1:]:
            c = comps.get((preset, pol, "none")); sp = split.get(f"{preset}/{pol}")
            if not c: continue
            lines.append(f"{PRESET_NAME[preset] if first else ''} & {names[pol]} & {fmt_ratio(c)} & {_p(c['p_holm'])} & {100 * c['bg_retained_median']:.0f}\\% & {sp['p_core_busy']:.0f}/{sp['e_core_busy']:.0f} \\\\" if sp else ""); first = False
        lines.append(r"\midrule" if preset != presets[-1] else r"\bottomrule")
    lines.append(r"\end{tabular}")
    (out / "hybrid.tex").write_text("\n".join(lines), encoding="utf-8")
    return summary


def write_markdown(s: dict[str, Any], path: Path) -> None:
    L = [f"# SmartSwap study {s['study_id']}", "", f"Host: {s['host']['cpu']}, {s['host']['logical_cpus']} logical CPUs, {s['host']['ram_gb']} GB, {s['host']['os']}, AC power: {s['host']['on_ac_power']}.",
         f"Trials: {s['trials']} ({s['trials_with_errors']} with errors), {s['blocks']} randomised blocks.", ""]
    hl = s.get("host_cpu_pct_before")
    if hl: L += [f"Non-study host CPU load just before trials: median {hl['median']:.0f}%, p90 {hl['p90']:.0f}%, max {hl['max']:.0f}%.", ""]
    L += ["## Interference validity (unmitigated vs. no-interference p95)", "", "| Workload | idle p95 | loaded p95 | inflation | Wilcoxon p |", "|---|---:|---:|---:|---:|"]
    for k, v in s["validity"].items(): L.append(f"| {PRESET_NAME[k]} | {v['idle_p95_median']:.2f} | {v['loaded_p95_median']:.2f} | {v['inflation']:.2f}x | {_p(v['p'])} |")
    L += ["", "## Paired comparisons (median per-block ratio, 95% bootstrap CI, Holm-adjusted exact Wilcoxon)", "", "| Workload | Policy | vs | metric | n | ratio [CI] | p (Holm) | BG kept |", "|---|---|---|---|---:|---|---:|---:|"]
    for c in s["comparisons"]:
        kept = f"{100 * c['bg_retained_median']:.0f}%" if c["bg_retained_median"] is not None else "-"
        L.append(f"| {PRESET_NAME[c['preset']]} | {POLICY_NAME[c['policy']]} | {POLICY_NAME[c['reference']]} | {c['metric']} | {c['n']} | {fmt_ratio(c)} | {_p(c['p_holm'])} | {kept} |")
    L += ["", "## Attribution (observe-only runs)", ""]
    for k, v in s["attribution_accuracy"].items():
        L.append(f"- {PRESET_NAME[k]}: {v['correct']}/{v['periods_above_threshold']} periods correct" + (f" ({100 * v['accuracy']:.1f}%)" if v["accuracy"] is not None else ""))
    if s["idle_false_engage_rate"] is not None: L.append(f"- No interference: {100 * s['idle_false_engage_rate']:.1f}% of periods above the engage threshold")
    wv = s["wsi_validity"]
    L += ["", "## Is WSI a proxy for foreground latency? (observe-only trials)", "", f"Spearman rho between trial-mean WSI and foreground p95: {wv['spearman_rho']:.2f} (p={_p(wv['spearman_p'])}, n={wv['n_trials']}).",
          f"False-positive rate at the engage threshold on no-interference periods: {100 * wv['fpr_at_engage']:.1f}%." if wv["fpr_at_engage"] is not None else ""]
    for k, r in wv["roc"].items(): L.append(f"- {PRESET_NAME[k]}: AUC {r['auc']:.3f}, detected in {100 * r['tpr_at_engage']:.1f}% of periods at the engage threshold" if r["tpr_at_engage"] is not None else f"- {PRESET_NAME[k]}: no data")
    led = s["ledger"]
    L += ["", "## Action ledger", "", f"{led['actions_verified']}/{led['actions']} actions verified by kernel read-back; {led['reverts_verified']}/{led['reverts']} reverts verified; {led['cap_rollbacks']} caps rolled back by the benefit check.", ""]
    L += ["## Host-load robustness (Spearman, pre-trial host CPU vs unmitigated p95)", ""]
    for k, v in s["host_load_vs_p95_spearman"].items(): L.append(f"- {PRESET_NAME[k]}: rho={v['rho']:.2f}, p={_p(v['p'])}, n={v['n']}")
    path.write_text("\n".join(L) + "\n", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(); ap.add_argument("study", type=Path); ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()
    design = json.loads((args.study / "manifest.json").read_text(encoding="utf-8")).get("design_name", "main")
    if design == "hybrid":
        analyse_hybrid(args.study, args.out or args.study / "analysis"); print((args.out or args.study / "analysis") / "hybrid_summary.json"); return
    if design == "slo-sweep":
        analyse_sweep(args.study, args.out or args.study / "analysis"); print((args.out or args.study / "analysis") / "sweep_summary.json"); return
    s = analyse(args.study, args.out or args.study / "analysis")
    print((args.out or args.study / "analysis") / "RESULTS.md")


if __name__ == "__main__":
    main()
