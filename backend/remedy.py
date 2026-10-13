"""Minimum intervention for a confirmed culprit, with an uncertainty-aware fallback.

Blame (blame.py) says *who*. This module decides *how little* to do to them.

Per-culprit action ladder, cheapest first
    priority  IDLE priority class / nice 19 (I/O: very-low I/O priority)
    eco       EcoQoS execution-speed throttling (Windows 11)
    cap50 .. cap10   hard CPU-rate cap on that application's job only
Unlike the v2 controller, the ladder runs on the culprit alone; innocent
background jobs are never touched, which is the point of blaming first.

Learning what each action buys (interference-aware policy selection)
    Every time an action has been in force for `eval_periods`, its benefit
    (drop in WSI) and its cost (share of the culprit's own CPU use lost) are
    recorded per (resource, action), both relative to the culprit's state
    when blame confirmed it (already under blame's priority hint). For a
    stall, the optimizer picks the cheapest action whose mean recorded benefit
    covers that baseline's gap to the SLO; actions with no record yet are
    tried in cost order. Benefits are measured before/after, so drifting load
    adds noise to the record; averaging over many episodes is what tames it. After
    the SLO has held for `relax_periods`, it steps one action back down to see
    whether a cheaper one now suffices (load may have dropped).

Uncertainty-aware safety
    - No action stronger than a priority hint is ever applied to an
      application that blame has not confirmed.
    - If an action makes the WSI *worse*, it is reverted at once.
    - If blame stays inconclusive while the stall persists for
      `fallback_periods`, the optimizer falls back to the safe static policy
      (priority hint on every managed job) instead of guessing.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any

LADDER = ("priority", "eco", "cap50", "cap25", "cap10")
# Only levers that act on the contended resource. CPU-rate caps and EcoQoS do nothing for a
# storage culprit (it barely uses CPU, so a cap looks free and is useless); its lever is I/O priority.
RESOURCE_LADDERS = {"cpu": LADDER, "mem": LADDER, "io": ("priority",)}
CAP_PERCENT = {"cap50": 50.0, "cap25": 25.0, "cap10": 10.0}


@dataclass
class Remedy:
    kind: str          # apply | revert | fallback | fallback_revert
    app: int | None    # None for the fallback, which covers every managed job
    action: str        # an entry of LADDER (priority means the resource-matched hint)
    reason: str


@dataclass
class Optimizer:
    slo_wsi: float = 3.0
    eval_periods: int = 8
    relax_periods: int = 25
    fallback_periods: int = 40
    ladder: tuple[str, ...] = LADDER          # the platform's full ladder (no "eco" off Windows 11)
    knowledge: dict[str, dict[str, list[float]]] = field(default_factory=dict)  # "resource/action" -> {"benefit": [...], "cost": [...]}
    level: dict[int, int] = field(default_factory=dict)        # app -> index into ladder currently applied
    pending: dict[int, int] = field(default_factory=dict)       # app -> periods the current action has been in force
    base: dict[int, tuple[float, float]] = field(default_factory=dict)  # app -> (WSI, CPU cores) when blame confirmed it
    calm: int = 0
    unresolved: int = 0
    fallback: bool = False
    log: list[dict[str, Any]] = field(default_factory=list)

    def expected(self, resource: str, action: str) -> tuple[float | None, float | None]:
        k = self.knowledge.get(f"{resource}/{action}")
        if not k or not k["benefit"]: return None, None
        return statistics.fmean(k["benefit"]), statistics.fmean(k["cost"]) if k["cost"] else None

    def allowed(self, resource: str, i: int) -> bool:
        return self.ladder[i] in RESOURCE_LADDERS.get(resource, ("priority",))

    def top(self, resource: str) -> int:
        return max(i for i in range(len(self.ladder)) if self.allowed(resource, i))

    def choose(self, resource: str, gap: float, start: int = 0) -> int | None:
        """Index of the cheapest resource-matched action expected to close `gap` WSI points
        (untried actions count as candidates); None when no such action is left."""
        candidates = [i for i in range(start, len(self.ladder)) if self.allowed(resource, i)]
        for i in candidates:
            benefit, _ = self.expected(resource, self.ladder[i])
            if benefit is None or benefit >= gap: return i
        return candidates[-1] if candidates else None

    def step(self, wsi_recent: float, resource: str, confirmed: list[int], usage: dict[int, float], inconclusive: bool) -> list[Remedy]:
        """One control period. `confirmed` are blame-confirmed culprits, `usage` their current CPU cores."""
        out: list[Remedy] = []
        self.calm = self.calm + 1 if wsi_recent <= self.slo_wsi else 0
        for app in [a for a in self.level if a not in confirmed]:  # blame released it (stall cleared or exonerated)
            out.append(Remedy("revert", app, self.ladder[self.level.pop(app)], "no longer a confirmed culprit")); self.pending.pop(app, None); self.base.pop(app, None)
        for app in confirmed:
            if app not in self.level:  # blame's own nudge is the priority hint; start the ladder there
                self.level[app] = 0; self.pending[app] = 0; self.base[app] = (wsi_recent, usage.get(app, 0.0)); continue
            base_wsi, base_use = self.base[app]
            if app in self.pending:
                self.pending[app] += 1
                if self.pending[app] >= self.eval_periods:
                    action = self.ladder[self.level[app]]; benefit = base_wsi - wsi_recent
                    cost = max(0.0, 1 - usage.get(app, 0.0) / base_use) if base_use > 0 else 0.0
                    k = self.knowledge.setdefault(f"{resource}/{action}", {"benefit": [], "cost": []}); k["benefit"].append(round(benefit, 3)); k["cost"].append(round(cost, 3))
                    self.log.append({"app": app, "resource": resource, "action": action, "benefit_wsi": round(benefit, 3), "cost": round(cost, 3)})
                    del self.pending[app]
                    if self.level[app] > 0 and wsi_recent > base_wsi + 1.0:  # worse than doing nothing extra: step straight back
                        self.level[app] -= 1; out.append(Remedy("apply", app, self.ladder[self.level[app]], f"{action} raised WSI by {-benefit:.1f}; reverted to {self.ladder[self.level[app]]}"))
                continue
            nxt = self.choose(resource, base_wsi - self.slo_wsi, self.level[app] + 1) if wsi_recent > self.slo_wsi and self.level[app] < self.top(resource) else None
            if nxt is not None:
                self.level[app] = nxt; self.pending[app] = 0
                out.append(Remedy("apply", app, self.ladder[nxt], f"WSI {wsi_recent:.1f}% > SLO: cheapest action expected to close {base_wsi - self.slo_wsi:.1f} points from the confirmed baseline"))
            elif self.calm >= self.relax_periods and self.level[app] > 0:
                self.level[app] -= 1; self.calm = 0; self.pending[app] = 0
                out.append(Remedy("apply", app, self.ladder[self.level[app]], f"SLO held for {self.relax_periods} periods: trying the cheaper {self.ladder[self.level[app]]}"))
        # uncertainty-aware fallback: a persistent stall that blame cannot pin on anyone
        self.unresolved = self.unresolved + 1 if (wsi_recent > self.slo_wsi and not confirmed and inconclusive) else 0
        if not self.fallback and self.unresolved >= self.fallback_periods:
            self.fallback = True; out.append(Remedy("fallback", None, "priority", f"stall unexplained for {self.unresolved} periods: safe static priority on all managed jobs"))
        elif self.fallback and (confirmed or self.calm >= self.relax_periods):
            self.fallback = False; out.append(Remedy("fallback_revert", None, "priority", "culprit confirmed or stall cleared"))
        return out
