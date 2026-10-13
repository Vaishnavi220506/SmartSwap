from backend.remedy import LADDER, Optimizer


def drive(opt: Optimizer, effect: dict[str, float], base: float, periods: int, confirmed=(7,), inconclusive=False):
    """Toy desktop: WSI = base minus the benefit of the culprit's current action; usage falls with stronger actions."""
    applied = []
    for _ in range(periods):
        action = LADDER[opt.level[7]] if 7 in opt.level else None
        wsi = max(0.0, base - (effect.get(action, 0.0) if action else 0.0))
        usage = {7: {None: 2.0, "priority": 2.0, "eco": 1.6, "cap50": 1.0, "cap25": 0.5, "cap10": 0.2}[action]}
        applied += opt.step(wsi, "cpu", list(confirmed), usage, inconclusive)
    return applied


def test_escalates_only_as_far_as_needed_and_learns_costs() -> None:
    opt = Optimizer(eval_periods=3, relax_periods=1000)
    drive(opt, {"priority": 1.0, "eco": 2.0, "cap50": 7.0, "cap25": 9.0, "cap10": 9.5}, base=9.0, periods=40)
    assert LADDER[opt.level[7]] == "cap50"  # the cheapest action that gets WSI (9 - 7 = 2) under the 3% SLO
    assert opt.expected("cpu", "cap50")[1] == 0.5  # half the culprit's work lost
    fresh = Optimizer(eval_periods=3, relax_periods=1000, knowledge=opt.knowledge)
    assert fresh.choose("cpu", 6.0, 1) == LADDER.index("cap50")  # next time it skips eco: known not to suffice


def test_an_action_that_makes_things_worse_is_undone() -> None:
    opt = Optimizer(eval_periods=3, relax_periods=1000)
    events = drive(opt, {"priority": 0.0, "eco": -3.0, "cap50": 8.0}, base=9.0, periods=30)
    assert any("raised WSI" in e.reason for e in events)
    assert opt.expected("cpu", "eco")[0] < 0 and LADDER[opt.level[7]] == "cap50"


def test_relaxes_to_a_cheaper_action_once_the_slo_holds() -> None:
    opt = Optimizer(eval_periods=2, relax_periods=5)
    drive(opt, {"priority": 0.0, "eco": 7.0}, base=9.0, periods=12)
    assert LADDER[opt.level[7]] == "eco"
    events = drive(opt, {"priority": 0.0, "eco": 7.0}, base=1.0, periods=10)  # load dropped
    assert any(e.action == "priority" and "cheaper" in e.reason for e in events)


def test_unexplained_stall_falls_back_to_safe_static_priority_never_caps() -> None:
    opt = Optimizer(fallback_periods=5)
    events = drive(opt, {}, base=9.0, periods=8, confirmed=(), inconclusive=True)
    assert [e.kind for e in events] == ["fallback"] and not opt.level
    events = drive(opt, {}, base=0.5, periods=30, confirmed=(), inconclusive=False)
    assert [e.kind for e in events] == ["fallback_revert"]


def test_storage_culprits_only_get_the_io_lever() -> None:
    opt = Optimizer(eval_periods=2, relax_periods=1000)
    events = []
    for _ in range(30): events += opt.step(9.0, "io", [7], {7: 0.05}, False)
    assert events == [] and opt.level[7] == 0  # no CPU caps or EcoQoS on a storage culprit, however long the stall
