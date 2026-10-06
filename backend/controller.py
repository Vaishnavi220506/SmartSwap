"""SmartSwap v2 control logic: stall index, differential attribution, and a
least-cost-first escalation ladder with AIMD CPU-rate control.

This module is pure decision logic (no Win32 calls) so it can be unit-tested and
replayed offline from recorded canary traces. `engine.py` executes its decisions.

Windows Stall Index (WSI)
    A user-mode analogue of Linux PSI "some" pressure. A low-duty canary wakes
    on a fixed period and times three components (scheduling delay, compute,
    memory stream, unbuffered 4 KiB read). Over a sliding window, the WSI is the
    share of wall time the canary lost to interference:
        WSI = 100 * sum_i max(0, latency_i - idle_latency) / window_ms
    No privileges, drivers, ETW sessions, or hardware counters are needed.

Differential attribution (specificity-ordered)
    Each component's inflation over its own idle calibration is computed. The
    signals are not equally specific: scheduling delay rises essentially only
    under CPU contention; the memory stream slows under bandwidth contention but
    barely under storage load; the unbuffered read slows under storage load AND
    under bandwidth contention (DMA competes for the memory controller). So the
    rule tests the most specific signal first: cpu, then mem, then io.

Escalation ladder (cheapest action first)
    level 1: resource-matched scheduler hint, which costs the background almost
             nothing while the foreground is idle between beats:
               cpu -> IDLE priority class
               mem -> IDLE priority class (bandwidth is not a scheduled
                      resource, yet on the hybrid P/E-core pilot host this hint
                      halved probe p95 at no throughput cost; chosen from
                      measurement, mechanism unconfirmed)
               io  -> very-low I/O priority
    level 1.5 (hybrid CPUs, optional): core-class steering - confine the
             governed group to efficiency-class-0 (E-) cores. The background
             keeps working, but the foreground gets the P-cores (and, on
             hosts with a separate low-power island, less cache sharing).
             Benefit-checked like the cap and undone if it does not help.
    level 2: Job Object hard CPU-rate cap, adapted by AIMD against the SLO,
             entered only if the SLO stays violated under level 1.

Benefit verification
    Each action is verified twice: the actuator reads kernel state back (the
    action took effect), and the controller checks the canary (the action
    helped). If the cap does not cut the stall index by `benefit_min_reduction`
    within `benefit_eval_periods`, it is rolled back as ineffective and not
    retried for `ineffective_cooldown_periods`. Hard caps duty-cycle a job, so
    they can trade away throughput without removing bursty interference.
    Load-aware release: the controller only lets go of hints once the managed
    group has gone quiet (SmartSwap knows which processes it governs), because
    "calm" under mitigation usually means the mitigation works, not that the
    load left. The costly cap is probed away by AIMD instead: when it climbs
    back to 100% it is removed and the ladder drops to level 1. If group
    activity is unknown, a calm hold that doubles on every relapse is used.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any

COMPONENTS = ("sched", "cpu", "mem", "io")
RESOURCES = ("cpu", "mem", "io")
HINTS = {"cpu": "priority", "mem": "priority", "io": "io_priority"}  # level-1 action per resource


@dataclass
class Calibration:
    """Idle medians per canary component (ms), measured with no background load."""

    sched: float
    cpu: float
    mem: float
    io: float

    @property
    def total(self) -> float:
        return self.sched + self.cpu + self.mem + self.io

    @classmethod
    def from_rows(cls, rows: list[dict[str, float]]) -> "Calibration":
        return cls(**{k: statistics.median(float(r[k]) for r in rows) for k in COMPONENTS})

    def as_dict(self) -> dict[str, float]:
        return {k: round(getattr(self, k), 4) for k in COMPONENTS}


@dataclass
class ControllerConfig:
    window_beats: int = 8              # sliding window of canary beats
    engage_wsi: float = 5.0            # % stalled time that engages control
    release_wsi: float = 2.0           # % stalled time considered calm
    engage_periods: int = 2            # consecutive hot periods before engaging
    release_periods: int = 25          # calm periods before releasing (doubles on each flap)
    release_periods_max: int = 50      # 10 s at the default 200 ms control period
    flap_window_periods: int = 25      # re-engaging this soon after a release counts as a flap
    slo_wsi: float = 3.0               # target the AIMD loop steers toward
    cap_initial: float = 50.0          # first hard cap, % of machine CPU
    cap_min: float = 5.0
    cap_max: float = 100.0
    md_factor: float = 0.6             # multiplicative decrease on SLO violation
    ai_step: float = 5.0               # additive increase per calm period
    md_holdoff_periods: int = 2        # let the window refill before cutting again
    ladder_hold_periods: int = 3       # consecutive SLO violations before escalating
    attribution_floor: float = 1.0     # component must be >= 2x its idle median
    benefit_eval_periods: int = 8      # periods at level 2 before judging the cap
    benefit_min_reduction: float = 0.3 # cap must cut mean WSI by >= 30% to stay
    ineffective_cooldown_periods: int = 50
    enable_aimd: bool = True           # ablation: False -> fixed cap_initial
    enable_benefit_check: bool = True  # ablation: False -> keep caps regardless
    enable_steer: bool = False         # hybrid CPUs: try E-core steering before capping
    enable_ladder: bool = True         # ablation: False -> jump straight to the cap
    act: bool = True                   # False -> observe only (overhead/attribution study)


def window_metrics(rows: list[dict[str, float]], cal: Calibration, period_ms: float) -> dict[str, Any]:
    """Compute WSI and per-component excess ratios over a window of canary beats."""
    if not rows: return {"wsi": 0.0, "excess": {r: 0.0 for r in RESOURCES}, "beats": 0}
    lost = sum(max(0.0, sum(float(r[k]) for k in COMPONENTS) - cal.total) for r in rows)
    wsi = min(100.0, 100.0 * lost / (len(rows) * period_ms))
    med = {k: statistics.median(float(r[k]) for r in rows) for k in COMPONENTS}
    floor = 0.05  # ms; keeps tiny idle medians from exploding the ratios
    excess = {
        "cpu": max(0.0, (med["sched"] - cal.sched) / max(cal.total, floor)),
        "mem": max(0.0, (med["mem"] - cal.mem) / max(cal.mem, floor)),
        "io": max(0.0, (med["io"] - cal.io) / max(cal.io, floor)),
    }
    return {"wsi": round(wsi, 3), "excess": {k: round(v, 3) for k, v in excess.items()}, "beats": len(rows), "median_ms": {k: round(v, 4) for k, v in med.items()}}


def attribute(excess: dict[str, float], floor: float) -> tuple[str, float]:
    """Name the interfering resource, testing the most specific signal first.

    Returns (resource, strength) where strength is that resource's excess ratio;
    "none" means no component crossed `floor` (e.g. a calm host or a transient).
    """
    for resource in RESOURCES:  # ordered by specificity: cpu, mem, io
        if excess[resource] >= floor: return resource, round(excess[resource], 3)
    return "none", 0.0


@dataclass
class Decision:
    kind: str            # hint | cap_set | cap_clear | hint_revert
    resource: str
    value: float | None
    reason: str


@dataclass
class Controller:
    cal: Calibration
    period_ms: float
    cfg: ControllerConfig = field(default_factory=ControllerConfig)
    engaged: bool = False
    level: int = 0
    resource: str = "none"
    cap: float | None = None
    hints: set[str] = field(default_factory=set)
    hot: int = 0
    calm: int = 0
    violating: int = 0
    since_change: int = 0
    since_md: int = 99
    release_hold: int = 0
    since_release: int = 10**6
    flaps: int = 0
    wsi_history: list[float] = field(default_factory=list)
    cap_baseline_wsi: float | None = None
    cap_periods: int = 0
    cap_blocked_until: int = -1
    period_index: int = 0
    rollbacks: int = 0
    steer_tried: bool = False
    steer_periods: int | None = None
    steer_baseline_wsi: float | None = None
    steer_rollbacks: int = 0
    trace: list[dict[str, Any]] = field(default_factory=list)

    def step(self, t_ms: float, rows: list[dict[str, float]], group_active: bool | None = None) -> list[Decision]:
        """Advance one control period given the most recent canary beats.

        `group_active` says whether the governed background processes are still
        consuming CPU or I/O (None when unknown).
        """
        m = window_metrics(rows[-self.cfg.window_beats:], self.cal, self.period_ms)
        wsi = m["wsi"]; resource, strength = attribute(m["excess"], self.cfg.attribution_floor)
        decisions: list[Decision] = []
        self.period_index += 1; self.wsi_history.append(wsi)
        if not self.release_hold: self.release_hold = self.cfg.release_periods
        self.since_change += 1; self.since_md += 1; self.since_release += 1
        self.hot = self.hot + 1 if wsi >= self.cfg.engage_wsi else 0
        self.calm = self.calm + 1 if wsi <= self.cfg.release_wsi else 0
        self.violating = self.violating + 1 if wsi > self.cfg.slo_wsi else 0

        if not self.engaged and self.hot >= self.cfg.engage_periods and resource != "none":
            if self.since_release <= self.cfg.flap_window_periods:
                # The stall came back right after we let go: the mitigation, not the
                # load, was what made it calm. Back off releases exponentially.
                self.flaps += 1; self.release_hold = min(self.cfg.release_periods_max, self.release_hold * 2)
            self.engaged = True; self.resource = resource; self.level = 0; self.since_change = 0
            decisions += self._escalate(f"engaged: WSI {wsi:.1f}% >= {self.cfg.engage_wsi}% for {self.hot} periods; attributed to {resource} (excess {strength:.2f}x)")
        elif self.engaged and self.calm >= self.release_hold and self.cap is None and group_active is not True:
            decisions += self._release(f"released: WSI {wsi:.1f}% <= {self.cfg.release_wsi}% for {self.calm} periods")
        elif self.engaged:
            violated = wsi > self.cfg.slo_wsi
            if resource not in ("none", self.resource) and violated and HINTS[resource] not in self.hints and self.cfg.enable_ladder:
                self.resource = resource  # interference changed character; add its matched hint
                decisions += self._hint(resource, f"re-attributed to {resource} (excess {strength:.2f}x)")
            if "steer" in self.hints and self.steer_periods is not None and self.cfg.enable_benefit_check:
                self.steer_periods += 1
                if self.steer_periods == self.cfg.benefit_eval_periods and self.steer_baseline_wsi:
                    after = statistics.fmean(self.wsi_history[-5:]); self.steer_periods = None
                    if after > (1 - self.cfg.benefit_min_reduction) * self.steer_baseline_wsi:
                        self.steer_rollbacks += 1; self.hints.discard("steer"); self.since_change = 0
                        decisions.append(Decision("hint_revert", "steer", None, f"benefit check failed: mean WSI {self.steer_baseline_wsi:.1f}% -> {after:.1f}%; E-core steering undone"))
            if self.level == 2 and self.cap is not None and self.cfg.enable_benefit_check:
                self.cap_periods += 1
                if self.cap_periods == self.cfg.benefit_eval_periods and self.cap_baseline_wsi:
                    after = statistics.fmean(self.wsi_history[-5:])
                    if after > (1 - self.cfg.benefit_min_reduction) * self.cap_baseline_wsi:
                        self.rollbacks += 1; self.cap = None; self.level = 1 if self.cfg.enable_ladder else 0; self.since_change = 0
                        self.cap_blocked_until = self.period_index + self.cfg.ineffective_cooldown_periods
                        decisions.append(Decision("cap_clear", self.resource, None, f"benefit check failed: mean WSI {self.cap_baseline_wsi:.1f}% -> {after:.1f}% (< {self.cfg.benefit_min_reduction:.0%} reduction); cap rolled back"))
                        self.trace.append(self._trace_row(t_ms, wsi, m, resource, strength, decisions))
                        return decisions if self.cfg.act else []
            if self.level < 2 and self.violating >= self.cfg.ladder_hold_periods and self.since_change >= self.cfg.ladder_hold_periods and self.period_index >= self.cap_blocked_until:
                decisions += self._escalate(f"SLO still violated (WSI {wsi:.1f}% > {self.cfg.slo_wsi}%) after level {self.level}")
            elif self.level == 2 and self.cfg.enable_aimd and self.cap is not None:
                if violated and self.since_md >= self.cfg.md_holdoff_periods and self.cap > self.cfg.cap_min:
                    new = max(self.cfg.cap_min, self.cap * self.cfg.md_factor)
                    decisions.append(self._set_cap(new, f"MD: WSI {wsi:.1f}% > SLO {self.cfg.slo_wsi}%")); self.since_md = 0
                elif not violated and self.cap < self.cfg.cap_max:
                    new = min(self.cfg.cap_max, self.cap + self.cfg.ai_step)
                    decisions.append(self._set_cap(new, f"AI: WSI {wsi:.1f}% within SLO"))
                elif not violated and self.cap >= self.cfg.cap_max:
                    decisions.append(Decision("cap_clear", self.resource, None, "AI reached 100%: cap removed, back to level 1"))
                    self.cap = None; self.level = 1 if self.cfg.enable_ladder else 0; self.since_change = 0

        self.trace.append(self._trace_row(t_ms, wsi, m, resource, strength, decisions))
        return decisions if self.cfg.act else []

    def _trace_row(self, t_ms: float, wsi: float, m: dict[str, Any], resource: str, strength: float, decisions: list[Decision]) -> dict[str, Any]:
        return {"t_ms": round(t_ms, 1), "release_hold": self.release_hold, "wsi": wsi, "excess": m["excess"], "attributed": resource, "strength": strength, "engaged": self.engaged, "level": self.level, "cap": self.cap, "steered": "steer" in self.hints, "decisions": [d.kind for d in decisions]}

    def _escalate(self, reason: str) -> list[Decision]:
        self.since_change = 0
        if self.level == 0 and self.cfg.enable_ladder:
            self.level = 1
            hint = self._hint(self.resource, reason)
            if hint: return hint
        if self.cfg.enable_steer and self.cfg.enable_ladder and not self.steer_tried and self.resource in ("cpu", "mem"):
            self.steer_tried = True; self.steer_periods = 0
            self.steer_baseline_wsi = statistics.fmean(self.wsi_history[-5:]) if self.wsi_history else None
            self.hints.add("steer")
            return [Decision("hint", "steer", None, reason + "; trying E-core steering before a cap")]
        self.level = 2; self.cap_periods = 0
        self.cap_baseline_wsi = statistics.fmean(self.wsi_history[-5:]) if self.wsi_history else None
        return [self._set_cap(self.cfg.cap_initial, reason)]

    def _hint(self, resource: str, reason: str) -> list[Decision]:
        hint = HINTS.get(resource)
        if hint and hint not in self.hints:
            self.hints.add(hint); return [Decision("hint", hint, None, reason)]
        return []

    def _set_cap(self, value: float, reason: str) -> Decision:
        self.cap = round(value, 2); return Decision("cap_set", self.resource, self.cap, reason)

    def _release(self, reason: str) -> list[Decision]:
        out = [Decision("cap_clear", self.resource, None, reason)] if self.cap is not None else []
        out += [Decision("hint_revert", h, None, reason) for h in sorted(self.hints)]
        self.engaged = False; self.level = 0; self.cap = None; self.hints = set(); self.resource = "none"; self.hot = 0; self.since_release = 0
        self.steer_tried = False; self.steer_periods = None
        return out
