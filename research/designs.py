"""Analysis of the v3 study designs: ecoqos, real, realapp, and blame.

    python -m research.analyze <study_dir>      (dispatches here by design name)

Same inference as the main study: paired by block, median per-block ratio with
a 95% bootstrap CI, exact Wilcoxon signed-rank p-values, Holm across the
design's family. Binary blame outcomes (was the first application nudged the
real culprit?) are compared with the exact McNemar test on discordant blocks.
"""
from __future__ import annotations

import json
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any

from backend.controller import Calibration, attribute, attribute_fingerprint
from backend.stats import holm, sign_test
from research.analyze import _p, compare, fmt_ratio, index, load, wsi_validity

NAMES = {"none": "None", "observe": "Observe-only", "nice": "Static priority", "ecoqos": "Efficiency mode (EcoQoS)", "adaptive": "SmartSwap v2",
         "adaptive_eco": "SmartSwap + EcoQoS rung", "oracle": "Oracle (culprit known)", "top_cpu": "Top-CPU app (Task Manager)",
         "passive": "Passive blame", "interventional": "One-at-a-time intervention", "factorial": "Randomized factorial (SmartSwap)"}
COMPARISONS = {
    "ecoqos": [("ecoqos", "none"), ("nice", "none"), ("adaptive", "ecoqos"), ("adaptive_eco", "ecoqos"), ("adaptive_eco", "adaptive")],
    "real": [("nice", "none"), ("ecoqos", "none"), ("adaptive", "none"), ("adaptive_eco", "none"), ("adaptive", "ecoqos"), ("adaptive_eco", "ecoqos")],
    "realapp": [("nice", "none"), ("adaptive", "none"), ("adaptive", "nice")],
}


def analyse_design(design: str, study: Path, out: Path) -> dict[str, Any]:
    out.mkdir(parents=True, exist_ok=True)
    if design == "attribution": return analyse_attribution(study, out)
    if design == "tradeoff": return analyse_tradeoff(study, out)
    return analyse_blame(study, out) if design == "blame" else analyse_policies(design, study, out)


def score_attribution(study: Path, engage: float = 5.0, floor: float = 1.0) -> dict[str, Any]:
    """Ordered rule vs. fingerprints on the *same* stalled periods (needs traces with median_ms)."""
    truth = {"cpu": "cpu", "membw": "mem", "io": "io"}; out: dict[str, Any] = {}; mixed_shares = []
    trials = [json.loads(l) for l in (study / "trials.jsonl").read_text().splitlines() if l.strip()]
    for t in trials:
        if t["policy"] != "observe" or t["errors"] or not t.get("fingerprints"): continue
        cal = Calibration(**t["calibration"])
        for r in t["controller_trace"]:
            if r["wsi"] < engage or not r.get("median_ms"): continue
            med = r["median_ms"]
            ex = {"cpu": max(0.0, (med["sched"] - cal.sched) / max(cal.total, 0.05)), "mem": max(0.0, (med["mem"] - cal.mem) / max(cal.mem, 0.05)), "io": max(0.0, (med["io"] - cal.io) / max(cal.io, 0.05))}
            rule = attribute(ex, floor)[0]; fp, _, shares = attribute_fingerprint(med, cal, t["fingerprints"], floor)
            if t["preset"] == "mixed": mixed_shares.append(shares)
            if t["preset"] in truth:
                c = out.setdefault(t["preset"], {"periods": 0, "rule_correct": 0, "fingerprint_correct": 0})
                c["periods"] += 1; c["rule_correct"] += rule == truth[t["preset"]]; c["fingerprint_correct"] += fp == truth[t["preset"]]
    for k in ("cpu", "membw", "io"):
        if k in out: out[k]["rule_accuracy"] = out[k]["rule_correct"] / out[k]["periods"]; out[k]["fingerprint_accuracy"] = out[k]["fingerprint_correct"] / out[k]["periods"]
    if mixed_shares: out["mixed_mean_shares"] = {k: round(statistics.fmean(s.get(k, 0.0) for s in mixed_shares), 3) for k in ("cpu", "mem", "io")}
    return out


def analyse_attribution(study: Path, out: Path) -> dict[str, Any]:
    """Ordered rule vs. this PC's learned fingerprints, scored on identical stalled periods."""
    manifest = json.loads((study / "manifest.json").read_text(encoding="utf-8"))
    sc = score_attribution(study, engage=manifest["controller"]["engage_wsi"], floor=manifest["controller"]["attribution_floor"])
    summary = {"study_id": manifest["study_id"], "design": "attribution", "host": manifest["host"], "scores": sc}
    (out / "summary.json").write_text(json.dumps(summary, indent=2, default=float), encoding="utf-8")
    L = [f"# Attribution: ordered rule vs. learned fingerprints ({manifest['study_id']})", "", f"Host: {manifest['host'].get('cpu')}.", "",
         "| Workload | stalled periods | ordered rule | fingerprints |", "|---|---:|---:|---:|"]
    L += [f"| {k} | {v['periods']} | {100 * v['rule_accuracy']:.1f}% | {100 * v['fingerprint_accuracy']:.1f}% |" for k, v in sc.items() if k in ("cpu", "membw", "io")]
    if "mixed_mean_shares" in sc: L += ["", f"Mixed workload (CPU + bandwidth + storage), mean fingerprint shares: {sc['mixed_mean_shares']}"]
    (out / "RESULTS.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    return summary


def analyse_policies(design: str, study: Path, out: Path) -> dict[str, Any]:
    manifest, trials = load(study); idx = index(trials)
    presets = sorted({p for p, _ in idx if p != "idle"})
    family = [c for preset in presets for a, b in COMPARISONS[design] if (c := compare(idx, preset, a, b))]
    for c, padj in zip(family, holm([c["p"] for c in family])): c["p_holm"] = padj
    validity = {}
    for preset in presets:  # does the workload interfere at all? (needs the idle control)
        blocks = sorted(set(idx[(preset, "none")]) & set(idx[("idle", "none")]))
        if len(blocks) >= 3:
            loaded = [idx[(preset, "none")][b]["foreground"]["p95_ms"] for b in blocks]; idle = [idx[("idle", "none")][b]["foreground"]["p95_ms"] for b in blocks]
            validity[preset] = {"n": len(blocks), "inflation": statistics.median(a / b for a, b in zip(loaded, idle)), "idle_p95_median": statistics.median(idle), "loaded_p95_median": statistics.median(loaded)}
    shares = [t["canary_cpu_share"] for t in trials if t.get("canary_cpu_share") is not None]
    wv = wsi_validity(trials, manifest["controller"]["engage_wsi"]) if any(t["policy"] == "observe" for t in trials) else None
    actions = [e for t in trials for e in t["actions"] if not e["kind"].endswith("_revert")]; reverts = [e for t in trials for e in t["actions"] if e["kind"].endswith("_revert")]
    summary = {"study_id": manifest["study_id"], "design": design, "host": manifest["host"], "foreground_mode": manifest.get("foreground_mode", "probe"),
               "trials": len(trials), "trials_with_errors": sum(1 for t in trials if t["errors"]), "comparisons": family, "validity": validity,
               "wsi_validity": {k: v for k, v in wv.items() if k != "points"} if wv else None,
               "canary_cpu_share_median": statistics.median(shares) if shares else None,
               "ui_timeouts": sum(t.get("ui_timeouts") or 0 for t in trials),
               "ledger": {"actions": len(actions), "verified": sum(e["verified"] for e in actions), "reverts": len(reverts), "reverts_verified": sum(e["verified"] for e in reverts)}}
    (out / "summary.json").write_text(json.dumps(summary, indent=2, default=float), encoding="utf-8")
    L = [f"# SmartSwap {design} study {summary['study_id']}", "", f"Host: {summary['host'].get('cpu')}, {summary['host'].get('logical_cpus')} logical CPUs. Foreground: {summary['foreground_mode']}.",
         f"Trials: {summary['trials']} ({summary['trials_with_errors']} with errors).", ""]
    if validity:
        L += ["## Interference validity (unmitigated vs. no interference)", "", "| Workload | idle p95 | loaded p95 | inflation |", "|---|---:|---:|---:|"]
        L += [f"| {k} | {v['idle_p95_median']:.2f} | {v['loaded_p95_median']:.2f} | {v['inflation']:.2f}x |" for k, v in validity.items()] + [""]
    if wv:
        L += [f"WSI vs. foreground p95 (observe-only trials): Spearman rho {wv['spearman_rho']:.2f} (p={_p(wv['spearman_p'])}, n={wv['n_trials']}).", ""]
    L += ["## Paired comparisons (p95; median per-block ratio [95% CI], Holm-adjusted exact Wilcoxon)", "", "| Workload | Policy | vs | n | ratio [CI] | p (Holm) | BG kept | blocks for 80% power |", "|---|---|---|---:|---|---:|---:|---:|"]
    for c in family:
        kept = f"{100 * c['bg_retained_median']:.0f}%" if c["bg_retained_median"] is not None else "-"
        L.append(f"| {c['preset']} | {NAMES.get(c['policy'], c['policy'])} | {NAMES.get(c['reference'], c['reference'])} | {c['n']} | {fmt_ratio(c)} | {_p(c['p_holm'])} | {kept} | {c['blocks_for_80pct_power'] or '-'} |")
    led = summary["ledger"]
    L += ["", f"Actions verified: {led['verified']}/{led['actions']}; reverts verified: {led['reverts_verified']}/{led['reverts']}.",
          f"Canary CPU use: {100 * summary['canary_cpu_share_median']:.2f}% of one core (median)." if summary["canary_cpu_share_median"] is not None else ""]
    (out / "RESULTS.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    lines = [r"\begin{tabular}{lllrr}", r"\toprule", r"Workload & Policy & vs. & p95 ratio [95\% CI] & $p_{\mathrm{Holm}}$ \\", r"\midrule"]
    lines += [f"{c['preset'].replace('_', ' ')} & {NAMES.get(c['policy'], c['policy'])} & {NAMES.get(c['reference'], c['reference'])} & {fmt_ratio(c)} & {_p(c['p_holm'])} \\\\" for c in family]
    (out / f"{design}.tex").write_text("\n".join(lines + [r"\bottomrule", r"\end{tabular}"]), encoding="utf-8")
    return summary


def blame_outcomes(trials: list[dict[str, Any]]) -> dict[tuple[str, str], dict[int, dict[str, Any]]]:
    out: dict[tuple[str, str], dict[int, dict[str, Any]]] = defaultdict(dict)
    for t in trials:
        if not t["errors"]: out[(t["scenario"], t["mode"])][t["block"]] = t
    return out


def precision_at_1(by_block: dict[int, dict[str, Any]]) -> dict[str, Any]:
    nudged = [t for t in by_block.values() if t["first_nudge_correct"] is not None]
    times = [t["time_to_correct_nudge_s"] for t in by_block.values() if t["time_to_correct_nudge_s"] is not None]
    return {"n": len(by_block), "nudged": len(nudged), "precision_at_1": sum(t["first_nudge_correct"] for t in nudged) / len(nudged) if nudged else None,
            "found_culprit": sum(1 for t in by_block.values() if t["time_to_correct_nudge_s"] is not None) / len(by_block) if by_block else None,
            "wrong_nudges_mean": statistics.fmean(t["wrong_nudges"] for t in by_block.values()) if by_block else None,
            "time_to_correct_s_median": statistics.median(times) if times else None,
            "p95_ms_median": statistics.median(t["foreground"]["p95_ms"] for t in by_block.values() if t["foreground"].get("p95_ms") is not None) if by_block else None}


def mcnemar(a: dict[int, dict[str, Any]], b: dict[int, dict[str, Any]]) -> dict[str, Any]:
    """Exact McNemar on 'first nudge hit the culprit', paired by block (no nudge counts as a miss)."""
    blocks = sorted(set(a) & set(b)); hit = lambda t: t["first_nudge_correct"] is True
    wins = sum(1 for k in blocks if hit(a[k]) and not hit(b[k])); losses = sum(1 for k in blocks if hit(b[k]) and not hit(a[k]))
    return {"n": len(blocks), "a_only": wins, "b_only": losses, "p": sign_test(wins, losses)}


def analyse_blame(study: Path, out: Path) -> dict[str, Any]:
    manifest, trials = load(study)
    for t in trials: t.setdefault("preset", t.get("scenario")); t.setdefault("policy", t.get("mode"))
    outcomes = blame_outcomes(trials); idx = index(trials)
    scenarios = sorted({s for s, _ in outcomes}); modes = [m for m in ["oracle", "top_cpu", "passive", "interventional", "factorial"] if any(mm == m for _, mm in outcomes)]
    best = "factorial" if "factorial" in modes else "interventional"
    cells = {f"{s}/{m}": precision_at_1(outcomes[(s, m)]) for s in scenarios for m in ["none", *modes] if outcomes[(s, m)]}
    family = []
    for s in scenarios:
        for ref in [m for m in ("top_cpu", "passive", "interventional") if m != best]:
            if outcomes[(s, best)] and outcomes[(s, ref)]:
                family.append({"test": "mcnemar_first_nudge", "scenario": s, "a": best, "b": ref, **mcnemar(outcomes[(s, best)], outcomes[(s, ref)])})
            c = compare(idx, s, best, ref)
            if c: family.append({"test": "wilcoxon_p95", "scenario": s, "a": best, "b": ref, **c})
        for m in modes:
            c = compare(idx, s, m, "none")
            if c: family.append({"test": "wilcoxon_p95", "scenario": s, "a": m, "b": "none", **c})
    for c, padj in zip(family, holm([c["p"] for c in family])): c["p_holm"] = padj
    verdicts = [v for t in trials if t["mode"] in ("interventional", "factorial") and not t["errors"] for v in t.get("verdicts", [])]
    bills = [v["bill_ms_per_s"] for v in verdicts if v.get("bill_ms_per_s") is not None and v["verdict"] == "confirmed" and v["correct"]]
    conf = [v for v in verdicts if v["verdict"] == "confirmed"]; exo = [v for v in verdicts if v["verdict"] == "exonerated"]
    pooled = {m: precision_at_1({i: t for i, t in enumerate(t for (s, mm), by in outcomes.items() if mm == m for t in by.values())}) for m in modes}
    overhead = [t["governor_cpu_share"] for t in trials if t.get("governor_cpu_share") is not None]
    actions = [e for t in trials for e in t["actions"] if not e["kind"].endswith("_revert")]; reverts = [e for t in trials for e in t["actions"] if e["kind"].endswith("_revert")]
    summary = {"study_id": manifest["study_id"], "design": "blame", "host": manifest["host"], "trials": len(trials), "trials_with_errors": sum(1 for t in trials if t["errors"]),
               "cells": cells, "pooled": pooled, "tests": family,
               "culprit_bill_ms_per_s_median": statistics.median(bills) if bills else None,
               "verdicts": {"confirmed": len(conf), "confirmed_correct": sum(v["correct"] for v in conf), "exonerated": len(exo), "exonerated_correct": sum(not v["correct"] for v in exo)},
               "blame_cpu_share_median": statistics.median(overhead) if overhead else None,
               "ledger": {"actions": len(actions), "verified": sum(e["verified"] for e in actions), "reverts": len(reverts), "reverts_verified": sum(e["verified"] for e in reverts)}}
    (out / "summary.json").write_text(json.dumps(summary, indent=2, default=float), encoding="utf-8")
    pct = lambda v: "-" if v is None else f"{100 * v:.0f}%"
    L = [f"# SmartSwap blame study {summary['study_id']}", "", f"Host: {summary['host'].get('cpu')}. Trials: {summary['trials']} ({summary['trials_with_errors']} with errors).", "",
         "## Who is the culprit? (first application nudged, per scenario)", "", "| Scenario | Mode | n | precision@1 | culprit found | wrong nudges | time to culprit (s) | p95 (ms) |", "|---|---|---:|---:|---:|---:|---:|---:|"]
    for key, c in cells.items():
        s, m = key.split("/")
        L.append(f"| {s} | {NAMES[m]} | {c['n']} | {pct(c['precision_at_1'])} | {pct(c['found_culprit'])} | {c['wrong_nudges_mean']:.2f} | {c['time_to_correct_s_median'] if c['time_to_correct_s_median'] is not None else '-'} | {c['p95_ms_median']:.2f} |")
    L += ["", "Pooled over scenarios: " + "; ".join(f"{NAMES[m]} {pct(v['precision_at_1'])}" for m, v in pooled.items()), "",
          "## Tests (Holm-adjusted across this family)", "", "| Scenario | Test | A | B | result | p (Holm) |", "|---|---|---|---|---|---:|"]
    for c in family:
        res = f"A-only {c['a_only']} vs B-only {c['b_only']} of {c['n']}" if c["test"].startswith("mcnemar") else f"p95 ratio {fmt_ratio(c)}"
        L.append(f"| {c['scenario']} | {c['test']} | {NAMES[c['a']]} | {NAMES[c['b']]} | {res} | {_p(c['p_holm'])} |")
    v = summary["verdicts"]; led = summary["ledger"]
    L += ["", f"Interventional verdicts: {v['confirmed_correct']}/{v['confirmed']} confirmations hit the culprit; {v['exonerated_correct']}/{v['exonerated']} exonerations cleared an innocent application.",
          f"Actions verified: {led['verified']}/{led['actions']}; reverts verified: {led['reverts_verified']}/{led['reverts']}.",
          f"Blame + controller CPU use: {100 * summary['blame_cpu_share_median']:.2f}% of one core (median)." if summary["blame_cpu_share_median"] is not None else ""]
    (out / "RESULTS.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    try: _blame_figure(cells, scenarios, modes, out)
    except ImportError: pass
    return summary


def _blame_figure(cells: dict[str, Any], scenarios: list[str], modes: list[str], out: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    tone = {"oracle": "#8a8984", "top_cpu": "#eb6834", "passive": "#2a78d6", "interventional": "#eda100", "factorial": "#1baf7a"}
    fig, ax = plt.subplots(figsize=(7.0, 2.2)); width = 0.8 / len(modes)
    for j, m in enumerate(modes):
        ys = [100 * (cells.get(f"{s}/{m}", {}).get("precision_at_1") or 0) for s in scenarios]
        ax.bar([i + (j - (len(modes) - 1) / 2) * width for i in range(len(scenarios))], ys, width=width * 0.95, color=tone[m], label=NAMES[m])
    ax.set_xticks(range(len(scenarios))); ax.set_xticklabels([s.replace("_", " ") for s in scenarios], fontsize=7); ax.set_ylim(0, 105); ax.set_ylabel("First nudge = culprit (%)", fontsize=7)
    ax.legend(fontsize=6.5, ncol=4, loc="upper center", bbox_to_anchor=(0.5, 1.22)); fig.tight_layout()
    fig.savefig(out / "fig_blame_precision.pdf"); fig.savefig(out / "fig_blame_precision.png"); plt.close(fig)


TRADEOFF_NAMES = {"none": "None", "nice_all": "Lower every job (static priority)", "ecoqos_all": "Efficiency mode on every job",
                  "adaptive_all": "SmartSwap v2 ladder on every job", "causalshield": "CausalShield (blame, then minimal fix)"}


def kept(t: dict[str, Any], ref: dict[str, Any], labels: list[str]) -> float | None:
    """Mean share of each named job's work kept vs. the unmitigated run of the same block."""
    a, b = t.get("app_units_per_s") or {}, ref.get("app_units_per_s") or {}
    ratios = [a.get(l, 0.0) / b[l] for l in labels if b.get(l)]
    return statistics.fmean(ratios) if ratios else None


def analyse_tradeoff(study: Path, out: Path) -> dict[str, Any]:
    """Go/no-go: does blaming first protect the foreground as well as lowering everyone, while keeping innocent work?"""
    from backend.stats import bootstrap_median_ci, wilcoxon_signed_rank
    manifest, trials = load(study)
    for t in trials: t.setdefault("preset", t.get("scenario")); t.setdefault("policy", t.get("mode"))
    idx = index(trials); out.mkdir(parents=True, exist_ok=True)
    scenarios = sorted({p for p, _ in idx}); policies = [q for q in TRADEOFF_NAMES if any(qq == q for _, qq in idx)]
    cells, family = {}, []
    for s in scenarios:
        ref = idx[(s, "none")]
        for q in policies:
            by = idx[(s, q)]; blocks = sorted(set(by) & set(ref))
            if not blocks: continue
            apps = by[blocks[0]].get("apps") or []; culprit = [a["label"] for a in apps if a["culprit"]]; innocent = [a["label"] for a in apps if not a["culprit"]]
            ik = [kept(by[b], ref[b], innocent) for b in blocks]; ck = [kept(by[b], ref[b], culprit) for b in blocks]
            ratio = [by[b]["foreground"]["p95_ms"] / ref[b]["foreground"]["p95_ms"] for b in blocks]
            cells[f"{s}/{q}"] = {"n": len(blocks), "p95_ratio_median": statistics.median(ratio), "innocent_kept_median": statistics.median([x for x in ik if x is not None] or [float("nan")]),
                                 "culprit_kept_median": statistics.median([x for x in ck if x is not None] or [float("nan")])}
        for base in ("nice_all", "adaptive_all", "ecoqos_all"):
            if not idx[(s, "causalshield")] or not idx[(s, base)]: continue
            blocks = sorted(set(idx[(s, "causalshield")]) & set(idx[(s, base)]) & set(ref))
            if len(blocks) < 3: continue
            innocent = [a["label"] for a in idx[(s, "causalshield")][blocks[0]].get("apps", []) if not a["culprit"]]
            d_keep = [(kept(idx[(s, "causalshield")][b], ref[b], innocent) or 0) - (kept(idx[(s, base)][b], ref[b], innocent) or 0) for b in blocks]
            family.append({"scenario": s, "vs": base, "metric": "innocent work kept (difference)", "n": len(blocks), "median": statistics.median(d_keep), "ci": bootstrap_median_ci(d_keep), "p": wilcoxon_signed_rank(d_keep)})
            c = compare(idx, s, "causalshield", base)
            if c: family.append({"scenario": s, "vs": base, "metric": "foreground p95 (ratio)", "n": c["n"], "median": c["median_ratio"], "ci": (c["ci_low"], c["ci_high"]), "p": c["p"]})
    for c, padj in zip(family, holm([c["p"] for c in family])): c["p_holm"] = padj
    summary = {"study_id": manifest["study_id"], "design": "tradeoff", "host": manifest["host"], "trials": len(trials), "trials_with_errors": sum(1 for t in trials if t["errors"]),
               "cells": cells, "tests": family,
               "remedy_knowledge": [t["shield"]["knowledge"] for t in trials if t.get("shield")][-1:] or None,
               "fallback_episodes": sum(t["shield"]["fallback_episodes"] for t in trials if t.get("shield"))}
    (out / "summary.json").write_text(json.dumps(summary, indent=2, default=float), encoding="utf-8")
    f = lambda v: "-" if v is None or v != v else f"{v:.2f}"
    L = [f"# Trade-off study {summary['study_id']}: protect the foreground, keep innocent work", "", f"Host: {manifest['host'].get('cpu')}. Trials: {summary['trials']} ({summary['trials_with_errors']} with errors).", "",
         "| Scenario | Policy | n | p95 vs none | innocent work kept | culprit work kept |", "|---|---|---:|---:|---:|---:|"]
    for key, c in cells.items():
        s, q = key.split("/"); L.append(f"| {s} | {TRADEOFF_NAMES[q]} | {c['n']} | {f(c['p95_ratio_median'])} | {f(c['innocent_kept_median'])} | {f(c['culprit_kept_median'])} |")
    L += ["", "## CausalShield vs. each blanket policy (paired by block, Holm-adjusted)", "", "| Scenario | vs | metric | median [95% CI] | p (Holm) |", "|---|---|---|---|---:|"]
    L += [f"| {c['scenario']} | {TRADEOFF_NAMES[c['vs']]} | {c['metric']} | {f(c['median'])} [{f(c['ci'][0])}, {f(c['ci'][1])}] | {_p(c['p_holm'])} |" for c in family]
    L += ["", f"Uncertainty fallbacks taken: {summary['fallback_episodes']}."]
    (out / "RESULTS.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    return summary
