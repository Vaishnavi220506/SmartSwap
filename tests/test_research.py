import json
import random
from pathlib import Path

from backend.engine import ALL_PRESETS, POLICIES, PRESETS, REAL_PRESETS, beat_latency, controller_config, summarize_rows
from backend.stats import blocks_for_power, hierarchical_bootstrap_median, sign_test
from research.designs import analyse_design
from research.pool import evaluate, load_studies
from research.run_study import DESIGNS, cells


def test_sign_test_is_exact() -> None:
    assert abs(sign_test(9, 1) - 22 / 1024) < 1e-12
    assert sign_test(0, 0) == 1.0 and sign_test(3, 3) == 1.0


def test_power_estimate_shrinks_with_cleaner_effects() -> None:
    noisy = [-0.3, 0.1, -0.5, -0.1, 0.2, -0.4]; clean = [-0.3, -0.31, -0.29, -0.3]
    assert blocks_for_power(clean) < blocks_for_power(noisy)
    assert blocks_for_power([0.1, -0.1, 0.0]) is None


def test_hierarchical_bootstrap_widens_with_host_disagreement() -> None:
    agree = hierarchical_bootstrap_median([[1.0, 1.1, 0.9], [1.0, 1.05, 0.95]], 2000)
    disagree = hierarchical_bootstrap_median([[0.2, 0.25, 0.3], [1.8, 1.9, 2.0]], 2000)
    assert (disagree[2] - disagree[1]) > (agree[2] - agree[1])


def test_ui_rows_use_the_app_round_trip() -> None:
    row = {"sched": 5.0, "cpu": 0.0, "mem": 0.0, "io": 0.0, "ui": 1.5}
    assert beat_latency(row) == 1.5 and beat_latency({"sched": 1, "cpu": 2, "mem": 3, "io": 4}) == 10
    assert summarize_rows([row] * 20, 50.0, 1000.0)["p95_ms"] == 1.5


def test_new_policies_and_designs_are_registered() -> None:
    assert {"ecoqos", "adaptive_eco"} <= set(POLICIES) and controller_config("adaptive_eco").enable_eco
    assert set(PRESETS) == {"idle", "cpu", "membw", "io", "mixed"} and set(REAL_PRESETS) <= set(ALL_PRESETS)
    for design in DESIGNS: assert cells(design)
    assert ("membw", "adaptive_eco") in cells("ecoqos") and ("idle", "observe") in cells("realapp")


def _trial(preset: str, policy: str, block: int, p95: float, **extra) -> dict:
    rows = [{"t": i * 50.0, "sched": 0.0, "cpu": p95, "mem": 0.0, "io": 0.0} for i in range(40)]
    return {"preset": preset, "policy": policy, "block": block, "errors": [], "foreground": summarize_rows(rows, 50.0, 2000.0), "foreground_rows": rows,
            "background_units_per_s": {"cpu": 10.0}, "actions": [{"kind": "priority_idle", "verified": True, "reason": ""}], "action_count": 1,
            "controller_trace": [{"wsi": p95, "attributed": "cpu"}] if policy == "observe" else [], "canary_cpu_share": 0.01, **extra}


def _study(tmp: Path, name: str, design: str, cpu: str, trials: list[dict]) -> Path:
    d = tmp / name; d.mkdir()
    (d / "manifest.json").write_text(json.dumps({"study_id": name, "design_name": design, "host": {"cpu": cpu, "logical_cpus": 8, "ram_gb": 16},
                                                  "foreground": {"SS_INTERVAL": "0.05"}, "controller": {"engage_wsi": 5.0}, "blocks": 6}), encoding="utf-8")
    (d / "trials.jsonl").write_text("\n".join(json.dumps(t) for t in trials), encoding="utf-8")
    return d


def test_design_analysis_and_multi_host_pooling(tmp_path: Path) -> None:
    rng = random.Random(3); dirs = []
    for host in ("hostA", "hostB", "hostC"):
        eco, blame = [], []
        for b in range(6):
            base = 20 + rng.random()
            eco += [_trial("membw", "none", b, base), _trial("membw", "ecoqos", b, base * 0.8), _trial("membw", "adaptive_eco", b, base * 0.5),
                    _trial("membw", "nice", b, base * 0.9), _trial("membw", "adaptive", b, base * 0.6)]
            for s in ("cpu", "io_vs_cpu"):
                for mode, ok in (("none", None), ("top_cpu", s == "cpu"), ("passive", True), ("interventional", True), ("factorial", True), ("oracle", True)):
                    t = _trial(s, mode, b, base * (0.4 if ok else 1.0), scenario=s, mode=mode, first_nudge_correct=ok, wrong_nudges=0 if ok in (None, True) else 1,
                               time_to_correct_nudge_s=1.0 if ok else None, verdicts=[], governor_cpu_share=0.02)
                    blame.append(t)
        dirs.append(_study(tmp_path, f"eco-{host}", "ecoqos", host, eco)); dirs.append(_study(tmp_path, f"blame-{host}", "blame", host, blame))
    s = analyse_design("ecoqos", dirs[0], tmp_path / "out-eco")
    assert any(c["policy"] == "adaptive_eco" and c["reference"] == "ecoqos" and c["median_ratio"] < 1 for c in s["comparisons"])
    b = analyse_design("blame", dirs[1], tmp_path / "out-blame")
    assert b["cells"]["io_vs_cpu/top_cpu"]["precision_at_1"] == 0 and b["cells"]["io_vs_cpu/interventional"]["precision_at_1"] == 1
    hyp = json.loads((Path(__file__).resolve().parents[1] / "research" / "hypotheses.json").read_text(encoding="utf-8"))
    rows = {r["id"]: r for r in evaluate(load_studies(dirs), hyp)}
    assert rows["H1"]["a_only"] == 18 and rows["H1"]["supported"]
    assert rows["H3"]["hosts"] == 3 and rows["H3"]["median_ratio"] < 1 and rows["H3"]["supported"]
    assert rows["H4"]["p"] == 1.0 and not rows["H4"]["supported"]  # no real-tool study supplied


def test_tradeoff_analysis_and_go_no_go_hypotheses(tmp_path: Path) -> None:
    rng = random.Random(9); dirs = []
    apps = [{"label": "culprit", "culprit": True}, {"label": "innocent1", "culprit": False}, {"label": "innocent2", "culprit": False}]
    for host in ("hostA", "hostB", "hostC"):
        trials = []
        for b in range(6):
            base = 20 + rng.random()
            # lowering everyone protects the foreground but slows the innocents; CausalShield slows only the culprit
            for pol, p95, inn, cul in (("none", base, 100, 100), ("nice_all", base * 0.5, 70, 60), ("adaptive_all", base * 0.45, 40, 30), ("causalshield", base * 0.52, 98, 50)):
                trials.append(_trial("cpu", pol, b, p95, scenario="cpu", mode=pol, apps=apps,
                                     app_units_per_s={"culprit": cul, "innocent1": inn, "innocent2": inn}, first_nudge_correct=None, wrong_nudges=0, time_to_correct_nudge_s=None))
        dirs.append(_study(tmp_path, f"tradeoff-{host}", "tradeoff", host, trials))
    s = analyse_design("tradeoff", dirs[0], tmp_path / "out-tradeoff")
    assert abs(s["cells"]["cpu/causalshield"]["innocent_kept_median"] - 0.98) < 1e-9 and abs(s["cells"]["cpu/nice_all"]["innocent_kept_median"] - 0.7) < 1e-9
    hyp = json.loads((Path(__file__).resolve().parents[1] / "research" / "hypotheses.json").read_text(encoding="utf-8"))
    rows = {r["id"]: r for r in evaluate(load_studies(dirs), hyp)}
    assert rows["H6"]["blocks"] == 18 and abs(rows["H6"]["median_diff"] - 0.28) < 1e-9 and rows["H6"]["supported"]
    assert rows["H7"]["median_ratio"] < 1.10 and rows["H7"]["supported"]  # 1.04 x: within the 10% margin
