import random

from backend.blame import Blamer, Interrogator, ProcSample, Suspect, app_roots
from backend.controller import Calibration, Controller, ControllerConfig

PERIOD_S = 0.2


def tree(cpu: dict[int, float], io: dict[int, float] | None = None) -> list[ProcSample]:
    """explorer(10) -> culprit launcher 100 (children 101, 102); decoy 200 (child 201); a lone app 300."""
    io = io or {}
    layout = [(10, 4, "explorer.exe"), (4, 0, "System"), (100, 10, "build.exe"), (101, 100, "cc.exe"), (102, 100, "cc.exe"),
              (200, 10, "indexer.exe"), (201, 200, "indexer.exe"), (300, 10, "chat.exe")]
    return [ProcSample(pid, ppid, name, cpu.get(pid, 0.0), io.get(pid, 0.0), 1.0) for pid, ppid, name in layout]


def test_processes_group_into_application_trees() -> None:
    roots = app_roots(tree({}))
    assert roots[101] == roots[102] == 100 and roots[201] == 200 and roots[300] == 300
    assert roots[10] == 10  # the shell is its own root, never the parent of an app
    assert app_roots(tree({}), stop_pids=frozenset({100}))[101] == 101


def test_reused_parent_pid_does_not_adopt_older_children() -> None:
    procs = [ProcSample(1, 0, "launcher.exe", 0, 0, create_time=50.0), ProcSample(2, 1, "child.exe", 0, 0, create_time=10.0)]
    assert app_roots(procs)[2] == 2


def feed(blamer: Blamer, periods: int, seed: int = 1) -> None:
    """Culprit (build tree) is on/off and drives the stall; the IDLE-priority indexer soaks up
    the CPU whenever the build is off (high total CPU, anti-correlated with the stall)."""
    rng = random.Random(seed); cum = {pid: 0.0 for pid in (100, 101, 102, 200, 201, 300)}
    for i in range(periods + 1):
        on = (i // 10) % 2 == 0
        use = {101: 0.9, 102: 0.9, 201: 0.3} if on else {101: 0.0, 102: 0.0, 201: 3.5}
        use[300] = 0.05
        for pid, cores in use.items(): cum[pid] += cores * PERIOD_S * (1 + 0.05 * rng.random())
        wsi = 12.0 + rng.random() if on else 0.5 * rng.random()
        blamer.update(tree(cum), wsi, {"cpu": 3.0 if on else 0.05, "mem": 0.0, "io": 0.0})


def test_passive_blame_ranks_the_stall_tracking_app_over_the_top_cpu_app() -> None:
    blamer = Blamer(PERIOD_S, horizon=50)
    feed(blamer, 49)
    ranked = blamer.rank("cpu")
    assert ranked[0].name == "build.exe" and ranked[0].rho > 0.5
    indexer = next(s for s in ranked if s.name == "indexer.exe")
    assert indexer.rho < 0 and indexer.score < ranked[0].score / 3
    assert blamer.top_cpu(window=50)[0].name == "indexer.exe"  # what Task Manager would blame


def test_protected_and_foreground_applications_are_flagged() -> None:
    blamer = Blamer(PERIOD_S)
    for i in range(3): blamer.update(tree({101: i * 0.2, 300: i * 0.2}), 10.0, {"cpu": 2.0, "mem": 0, "io": 0}, foreground=300)
    flags = {s.name: s.protected for s in blamer.rank("cpu")}
    assert flags["chat.exe"] is True and flags["build.exe"] is False


def suspect(app: int, score: float, protected: bool = False) -> Suspect:
    return Suspect(app, f"app{app}", [app], score, score, 0.0, 1.0, 1.0, protected)


def test_interventional_mode_exonerates_then_confirms() -> None:
    it = Interrogator("interventional", eval_periods=3)
    ranked = [suspect(1, 0.9), suspect(2, 0.5)]
    wsi = [10.0] * 6
    d = it.step(True, "cpu", wsi, ranked)
    assert [(x.kind, x.app, x.hint) for x in d] == [("nudge", 1, "priority")]
    for _ in range(2): assert it.step(True, "cpu", wsi, ranked) == []
    d = it.step(True, "cpu", wsi, ranked)  # stall unchanged under the nudge: app 1 is innocent
    assert [(x.kind, x.app) for x in d] == [("release", 1)] and it.verdicts[-1]["verdict"] == "exonerated"
    d = it.step(True, "cpu", wsi, ranked)
    assert [(x.kind, x.app) for x in d] == [("nudge", 2)]
    for _ in range(3): d = it.step(True, "cpu", wsi + [1.0] * 5, ranked)  # WSI fell: app 2 confirmed, stays nudged
    assert d == [] and it.verdicts[-1]["verdict"] == "confirmed" and it.nudged == {2: "priority"}
    assert [(x.kind, x.app) for x in it.step(False, "cpu", [0.0] * 5, ranked)] == [("release", 2)]


def test_protected_suspects_are_never_nudged_and_storage_gets_io_hint() -> None:
    it = Interrogator("passive")
    d = it.step(True, "io", [10.0] * 5, [suspect(1, 0.9, protected=True), suspect(2, 0.4)])
    assert [(x.kind, x.app, x.hint) for x in d] == [("nudge", 2, "io_priority")]


def test_nothing_happens_within_the_slo() -> None:
    assert Interrogator("interventional").step(True, "cpu", [1.0] * 5, [suspect(1, 0.9)]) == []


def test_eco_rung_is_tried_before_the_cap_and_rolled_back_if_useless() -> None:
    cal = Calibration(sched=0.3, cpu=0.25, mem=0.45, io=0.25)
    ctl = Controller(cal, 40.0, ControllerConfig(enable_eco=True))
    rows = [{"sched": 0.3, "cpu": 0.25, "mem": 3.0, "io": 0.25}] * 8
    kinds = []
    for i in range(2 + 3 + 8):
        kinds += [d.kind + ":" + d.resource for d in ctl.step(i * 200.0, rows, True)]
    assert kinds[:2] == ["hint:priority", "hint:eco"]
    assert "hint_revert:eco" in kinds and ctl.rung_rollbacks == {"eco": 1}


def simulate(it, base, effects: dict[int, float], ranked: list[Suspect], periods: int) -> list[float]:
    """Drive an interrogator against a toy desktop: WSI = base(t) + effect of every un-nudged culprit."""
    history: list[float] = []
    for t in range(periods):
        wsi = base(t) + sum(e for app, e in effects.items() if app not in it.nudged)
        history.append(wsi)
        it.step(True, "cpu", history, ranked)
    return history


def test_factorial_confirms_the_culprit_and_clears_the_innocent() -> None:
    from backend.blame import FactorialInterrogator
    it = FactorialInterrogator(seed=1)
    simulate(it, lambda t: 2.0, {1: 8.0}, [suspect(2, 0.9), suspect(1, 0.5)], 40)
    verdicts = {v["app"]: v for v in it.verdicts}
    assert verdicts[1]["verdict"] == "confirmed" and verdicts[1]["bill_ms_per_s"] > 50
    assert verdicts[2]["verdict"] == "exonerated" and it.nudged == {1: "priority"}


def test_randomization_resists_a_load_drop_that_fools_the_one_at_a_time_test() -> None:
    from backend.blame import FactorialInterrogator
    drop = lambda t: 12.0 if t < 6 else 3.0          # background load halves right after testing starts
    ranked = [suspect(2, 0.9), suspect(1, 0.5)]      # the innocent app is ranked first
    seq = Interrogator("interventional", eval_periods=8)
    simulate(seq, drop, {1: 4.0}, ranked, 12)
    assert seq.verdicts[0]["app"] == 2 and seq.verdicts[0]["verdict"] == "confirmed"  # fooled by the drop
    wrong = 0
    for seed in range(40):
        fac = FactorialInterrogator(seed=seed)
        simulate(fac, lambda t: 12.0 if t < 10 else 3.0, {1: 4.0}, ranked, 40)
        wrong += any(v["app"] == 2 and v["verdict"] == "confirmed" for v in fac.verdicts)
    assert wrong <= 6  # about the 5% false-positive rate the permutation test promises, not 100%
