from backend.controller import Calibration, Controller, ControllerConfig, attribute, window_metrics

CAL = Calibration(sched=0.3, cpu=0.25, mem=0.45, io=0.25)
PERIOD = 40.0


def beats(n: int, sched: float = 0.3, cpu: float = 0.25, mem: float = 0.45, io: float = 0.25) -> list[dict[str, float]]:
    return [{"sched": sched, "cpu": cpu, "mem": mem, "io": io} for _ in range(n)]


def test_idle_window_has_no_stall_and_no_attribution() -> None:
    m = window_metrics(beats(8), CAL, PERIOD)
    assert m["wsi"] == 0.0
    assert attribute(m["excess"], 1.0) == ("none", 0.0)


def test_wsi_is_share_of_wall_time_lost() -> None:
    # Each 40 ms beat loses 4 ms to scheduling delay -> 10% of wall time.
    m = window_metrics(beats(8, sched=4.3), CAL, PERIOD)
    assert abs(m["wsi"] - 10.0) < 1e-6


def test_attribution_prefers_the_most_specific_signal() -> None:
    # Bandwidth contention also slows the unbuffered read (larger ratio), but the
    # memory stream is the more specific signal, so it must win.
    m = window_metrics(beats(8, mem=1.5, io=1.7), CAL, PERIOD)
    assert m["excess"]["io"] > m["excess"]["mem"]
    assert attribute(m["excess"], 1.0)[0] == "mem"
    assert attribute(window_metrics(beats(8, sched=8.0, io=1.3), CAL, PERIOD)["excess"], 1.0)[0] == "cpu"
    assert attribute(window_metrics(beats(8, io=2.8), CAL, PERIOD)["excess"], 1.0)[0] == "io"


def run(ctl: Controller, rows: list[dict[str, float]], periods: int, active: bool | None = True) -> list[str]:
    kinds = []
    for i in range(periods):
        kinds += [d.kind + (":" + d.resource if d.kind.startswith("hint") else "") for d in ctl.step(i * 200.0, rows, active)]
    return kinds


def test_engages_after_sustained_stall_with_matched_hint() -> None:
    ctl = Controller(CAL, PERIOD)
    assert run(ctl, beats(8, sched=8.0), 1) == []
    assert run(ctl, beats(8, sched=8.0), 1) == ["hint:priority"]
    assert ctl.engaged and ctl.level == 1
    io_ctl = Controller(CAL, PERIOD)
    assert run(io_ctl, beats(8, io=5.0), 2) == ["hint:io_priority"]


def test_escalates_to_cap_only_after_sustained_violation_then_aimd() -> None:
    ctl = Controller(CAL, PERIOD)
    kinds = run(ctl, beats(8, sched=8.0), 6)
    assert kinds[0] == "hint:priority" and "cap_set" in kinds
    assert ctl.level == 2 and ctl.cap is not None and ctl.cap < 50.0  # MD cut below the initial cap
    before = ctl.cap
    run(ctl, beats(8), 3)  # calm: additive increase
    assert ctl.cap > before


def test_ineffective_cap_is_rolled_back_by_benefit_check() -> None:
    ctl = Controller(CAL, PERIOD, ControllerConfig(enable_aimd=False))
    # engage after 2 hot periods, cap after 3 sustained violations, judged 8 periods later
    kinds = run(ctl, beats(8, mem=3.0), 2 + 3 + 8)  # stall never improves under the cap
    assert "cap_set" in kinds and kinds[-1] == "cap_clear"
    assert ctl.rollbacks == 1 and ctl.cap is None and ctl.level == 1
    # Cooldown: the cap is not retried immediately.
    assert "cap_set" not in run(ctl, beats(8, mem=3.0), 10)


def test_effective_cap_survives_benefit_check() -> None:
    ctl = Controller(CAL, PERIOD, ControllerConfig(enable_aimd=False))
    run(ctl, beats(8, sched=8.0), 5)
    assert ctl.cap is not None
    run(ctl, beats(8, sched=0.35), 9)
    assert ctl.rollbacks == 0 and ctl.cap is not None


def test_release_waits_for_the_governed_group_to_go_quiet() -> None:
    ctl = Controller(CAL, PERIOD)
    run(ctl, beats(8, sched=8.0), 2)
    assert run(ctl, beats(8), 60, active=True) == []  # calm because of the hint, load still there
    assert ctl.engaged
    assert run(ctl, beats(8), 1, active=False) == ["hint_revert:priority"]
    assert not ctl.engaged


def test_flap_backoff_doubles_release_hold_when_activity_unknown() -> None:
    ctl = Controller(CAL, PERIOD)
    run(ctl, beats(8, sched=8.0), 2, active=None)
    run(ctl, beats(8), ctl.cfg.release_periods, active=None)
    assert not ctl.engaged
    run(ctl, beats(8, sched=8.0), 2, active=None)  # relapse right after release
    assert ctl.flaps == 1 and ctl.release_hold == 2 * ctl.cfg.release_periods


def test_observe_mode_decides_but_never_acts() -> None:
    ctl = Controller(CAL, PERIOD, ControllerConfig(act=False))
    assert run(ctl, beats(8, sched=8.0), 8) == []
    assert any(row["decisions"] for row in ctl.trace)


def test_noladder_ablation_goes_straight_to_the_cap() -> None:
    ctl = Controller(CAL, PERIOD, ControllerConfig(enable_ladder=False))
    assert run(ctl, beats(8, sched=8.0), 2) == ["cap_set"]


def test_hybrid_ladder_steers_to_ecores_before_capping() -> None:
    ctl = Controller(CAL, PERIOD, ControllerConfig(enable_steer=True))
    kinds = run(ctl, beats(8, sched=8.0), 2 + 3 + 3 + 1)
    assert kinds[:2] == ["hint:priority", "hint:steer"]
    assert "cap_set" in kinds and kinds.index("cap_set") > kinds.index("hint:steer")


def test_ineffective_steering_is_undone_by_benefit_check() -> None:
    ctl = Controller(CAL, PERIOD, ControllerConfig(enable_steer=True))
    kinds = run(ctl, beats(8, mem=3.0), 2 + 3 + 8)  # stall unchanged after steering
    assert "hint:steer" in kinds and "hint_revert:steer" in kinds
    assert ctl.steer_rollbacks == 1 and "steer" not in ctl.hints


def test_storage_interference_never_steers() -> None:
    ctl = Controller(CAL, PERIOD, ControllerConfig(enable_steer=True))
    assert "hint:steer" not in run(ctl, beats(8, io=8.0), 12)
