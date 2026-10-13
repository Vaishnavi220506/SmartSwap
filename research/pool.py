"""Pool block studies from several machines and test the pre-registered hypotheses.

    python -m research.pool <study_dir> [<study_dir> ...] [--hypotheses research/hypotheses.json] [--out pooled]

Blocks are nested in hosts, so blocks from different machines are not treated
as one big sample. For every paired comparison present on two or more hosts:
  - per-host median ratio (each host is its own replication);
  - pooled median ratio with a hierarchical bootstrap CI (resample hosts, then
    blocks within host), which widens with between-host disagreement;
  - host-level exact sign test: on how many machines did A beat B?
  - all-block exact Wilcoxon on log ratios (valid under exchangeability of
    blocks; reported next to the host-level test, never instead of it).
The primary family is the pre-registered hypotheses file; Holm runs over it alone.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any

from backend.stats import hierarchical_bootstrap_median, holm, sign_test, spearman, wilcoxon_signed_rank
from research.analyze import index, load

ROOT = Path(__file__).resolve().parent


def host_key(manifest: dict[str, Any]) -> str:
    h = manifest.get("host", {})
    return f"{h.get('cpu', '?')} | {h.get('logical_cpus', '?')} LP | {h.get('ram_gb', '?')} GB"


def load_studies(dirs: list[Path]) -> list[dict[str, Any]]:
    studies = []
    for d in dirs:
        manifest, trials = load(d)
        for t in trials:  # blame trials name their cells by scenario/mode
            t.setdefault("preset", t.get("scenario")); t.setdefault("policy", t.get("mode"))
        studies.append({"dir": str(d), "design": manifest.get("design_name", "main"), "host": host_key(manifest), "trials": trials, "idx": index(trials)})
    return studies


def block_ratios(idx: Any, preset: str, a: str, b: str, metric: str = "p95_ms") -> list[float]:
    blocks = sorted(set(idx[(preset, a)]) & set(idx[(preset, b)]))
    return [idx[(preset, a)][k]["foreground"][metric] / idx[(preset, b)][k]["foreground"][metric] for k in blocks if idx[(preset, b)][k]["foreground"][metric] > 0]


def pooled_ratio(studies: list[dict[str, Any]], design: str, preset: str, a: str, b: str, direction: str = "two-sided") -> dict[str, Any] | None:
    by_host: dict[str, list[float]] = defaultdict(list)
    for s in studies:
        if s["design"] == design: by_host[s["host"]] += block_ratios(s["idx"], preset, a, b)
    groups = [v for v in by_host.values() if v]
    if not groups: return None
    med, lo, hi = hierarchical_bootstrap_median([[math.log(r) for r in g] for g in groups])
    host_medians = {h: statistics.median(v) for h, v in by_host.items() if v}
    wins = sum(1 for m in host_medians.values() if m < 1); losses = sum(1 for m in host_medians.values() if m > 1)
    logs = [math.log(r) for g in groups for r in g]
    p = wilcoxon_signed_rank(logs)
    if direction == "less": p = p / 2 if statistics.median(logs) < 0 else 1 - p / 2
    elif direction == "greater": p = p / 2 if statistics.median(logs) > 0 else 1 - p / 2
    return {"design": design, "preset": preset, "a": a, "b": b, "hosts": len(groups), "blocks": len(logs), "median_ratio": math.exp(med), "ci_low": math.exp(lo), "ci_high": math.exp(hi),
            "host_medians": host_medians, "hosts_a_better": wins, "hosts_b_better": losses, "host_sign_p": sign_test(wins, losses), "p": p}


def pooled_mcnemar(studies: list[dict[str, Any]], a: str, b: str, scenario: str = "*") -> dict[str, Any]:
    wins = losses = n = 0; hosts = set()
    for s in studies:
        if s["design"] != "blame": continue
        for (sc, mode), by_block in s["idx"].items():
            if mode != a or (scenario != "*" and sc != scenario): continue
            other = s["idx"].get((sc, b), {})
            for blk in set(by_block) & set(other):
                ha, hb = by_block[blk]["first_nudge_correct"] is True, other[blk]["first_nudge_correct"] is True
                n += 1; wins += ha and not hb; losses += hb and not ha; hosts.add(s["host"])
    return {"pairs": n, "hosts": len(hosts), "a_only": wins, "b_only": losses, "p": sign_test(wins, losses) / 2 if wins > losses else 1.0}  # one-sided: A better


def pooled_spearman(studies: list[dict[str, Any]], design: str, policy: str) -> dict[str, Any]:
    pts = []
    for s in studies:
        if s["design"] != design: continue
        for t in s["trials"]:
            if t["policy"] == policy and not t["errors"] and t.get("controller_trace") and t["foreground"].get("p95_ms"):
                pts.append((statistics.fmean(r["wsi"] for r in t["controller_trace"]), t["foreground"]["p95_ms"]))
    if len(pts) < 5: return {"n": len(pts), "rho": None, "p": 1.0}
    rho, p = spearman([x for x, _ in pts], [y for _, y in pts])
    return {"n": len(pts), "rho": rho, "p": p / 2 if rho > 0 else 1 - p / 2}  # one-sided: positive association


def pooled_innocent_kept(studies: list[dict[str, Any]], a: str, b: str) -> dict[str, Any]:
    """Paired difference in innocent work kept (A - B, vs. the same block's unmitigated run), blocks nested in hosts."""
    from research.designs import kept
    by_host: dict[str, list[float]] = defaultdict(list)
    for s in studies:
        if s["design"] != "tradeoff": continue
        for (sc, pol), by in s["idx"].items():
            if pol != a: continue
            other, ref = s["idx"].get((sc, b), {}), s["idx"].get((sc, "none"), {})
            for blk in set(by) & set(other) & set(ref):
                innocent = [x["label"] for x in by[blk].get("apps", []) if not x["culprit"]]
                ka, kb = kept(by[blk], ref[blk], innocent), kept(other[blk], ref[blk], innocent)
                if ka is not None and kb is not None: by_host[s["host"]].append(ka - kb)
    diffs = [v for g in by_host.values() for v in g]
    if not diffs: return {"p": 1.0, "missing": True}
    med, lo, hi = hierarchical_bootstrap_median(list(by_host.values()))
    p = wilcoxon_signed_rank(diffs); p = p / 2 if statistics.median(diffs) > 0 else 1 - p / 2
    return {"hosts": len(by_host), "blocks": len(diffs), "median_diff": med, "ci_low": lo, "ci_high": hi, "p": p}


def pooled_noninferior(studies: list[dict[str, Any]], a: str, b: str, margin: float) -> dict[str, Any]:
    """One-sided test that p95(A)/p95(B) < margin, on log ratios shifted by log(margin)."""
    by_host: dict[str, list[float]] = defaultdict(list)
    for s in studies:
        if s["design"] != "tradeoff": continue
        for sc in {p for p, _ in s["idx"]}: by_host[s["host"]] += block_ratios(s["idx"], sc, a, b)
    logs = [math.log(r) for g in by_host.values() for r in g]
    if not logs: return {"p": 1.0, "missing": True}
    shifted = [x - math.log(margin) for x in logs]; p = wilcoxon_signed_rank(shifted)
    p = p / 2 if statistics.median(shifted) < 0 else 1 - p / 2
    med, lo, hi = hierarchical_bootstrap_median([[math.log(r) for r in g] for g in by_host.values() if g])
    return {"hosts": len(by_host), "blocks": len(logs), "median_ratio": math.exp(med), "ci_low": math.exp(lo), "ci_high": math.exp(hi), "margin": margin, "p": p}


def evaluate(studies: list[dict[str, Any]], hypotheses: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for h in hypotheses["hypotheses"]:
        if h["test"] == "mcnemar_first_nudge": r = pooled_mcnemar(studies, h["a"], h["b"], h.get("scenario", "*"))
        elif h["test"] == "spearman_wsi_p95": r = pooled_spearman(studies, h["design"], h["policy"])
        elif h["test"] == "innocent_kept": r = pooled_innocent_kept(studies, h["a"], h["b"])
        elif h["test"] == "noninferior_p95": r = pooled_noninferior(studies, h["a"], h["b"], h["margin"])
        else: r = pooled_ratio(studies, h["design"], h["preset"], h["a"], h["b"], h.get("direction", "two-sided")) or {"p": 1.0, "missing": True}
        rows.append({**h, **r})
    for r, padj in zip(rows, holm([r["p"] for r in rows])): r["p_holm"] = padj; r["supported"] = padj < hypotheses.get("alpha", 0.05)
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("studies", type=Path, nargs="+"); ap.add_argument("--hypotheses", type=Path, default=ROOT / "hypotheses.json"); ap.add_argument("--out", type=Path, default=Path("pooled"))
    args = ap.parse_args()
    studies = load_studies(args.studies); hyp = json.loads(args.hypotheses.read_text(encoding="utf-8"))
    primary = evaluate(studies, hyp)
    secondary = []
    for design in sorted({s["design"] for s in studies} - {"blame"}):
        cells = {(p, q) for s in studies if s["design"] == design for p, q in s["idx"]}
        for preset in sorted({p for p, _ in cells}):
            for a in sorted({q for p, q in cells if p == preset} - {"none"}):
                r = pooled_ratio(studies, design, preset, a, "none")
                if r and r["hosts"] >= 2: secondary.append(r)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "pooled.json").write_text(json.dumps({"hosts": sorted({s["host"] for s in studies}), "primary": primary, "secondary": secondary}, indent=2, default=float), encoding="utf-8")
    L = ["# Pooled multi-host results", "", "Hosts: " + "; ".join(sorted({s["host"] for s in studies})), "", "## Pre-registered primary hypotheses (Holm over these only)", "",
         "| ID | Claim | Evidence | p (Holm) | Supported |", "|---|---|---|---:|---|"]
    for r in primary:
        if r["test"] == "mcnemar_first_nudge": ev = f"{r['a_only']} vs {r['b_only']} discordant of {r['pairs']} pairs, {r['hosts']} hosts"
        elif r["test"] == "spearman_wsi_p95": ev = f"rho {r['rho']:.2f}, n={r['n']}" if r["rho"] is not None else "no data"
        elif r.get("missing"): ev = "no data"
        elif r["test"] == "innocent_kept": ev = f"innocent work kept +{100 * r['median_diff']:.0f} pts [{100 * r['ci_low']:.0f}, {100 * r['ci_high']:.0f}], {r['blocks']} blocks, {r['hosts']} hosts"
        elif r["test"] == "noninferior_p95": ev = f"p95 ratio {r['median_ratio']:.2f} [{r['ci_low']:.2f}, {r['ci_high']:.2f}] vs margin {r['margin']:.2f}, {r['blocks']} blocks"
        else: ev = f"ratio {r['median_ratio']:.2f} [{r['ci_low']:.2f}, {r['ci_high']:.2f}], {r['hosts_a_better']}/{r['hosts']} hosts better, {r['blocks']} blocks"
        L.append(f"| {r['id']} | {r['claim']} | {ev} | {r['p_holm']:.4f} | {'yes' if r['supported'] else 'no'} |")
    if secondary:
        L += ["", "## Secondary (vs. no mitigation, hierarchical bootstrap; not multiplicity-adjusted)", "", "| Design | Workload | Policy | hosts | ratio [95% CI] | hosts better |", "|---|---|---|---:|---|---:|"]
        L += [f"| {r['design']} | {r['preset']} | {r['a']} | {r['hosts']} | {r['median_ratio']:.2f} [{r['ci_low']:.2f}, {r['ci_high']:.2f}] | {r['hosts_a_better']}/{r['hosts']} |" for r in secondary]
    (args.out / "POOLED.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    print(args.out / "POOLED.md")


if __name__ == "__main__":
    main()
