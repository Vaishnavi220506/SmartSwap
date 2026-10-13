"""CausalShield: detect -> blame -> minimum intervention -> fall back when unsure.

SmartSwap is the sensing and actuation layer (canary WSI, attribution,
verified controls). CausalShield sits on top of it and only ever acts on the
background jobs a user has handed to it (launched through it, or opted in):

    detect   Controller (observe mode): WSI, engagement, contended resource
    blame    Blamer ranks suspects; FactorialInterrogator runs randomized
             micro-experiments and confirms culprits (blame.py)
    remedy   Optimizer escalates only the confirmed culprit, to the cheapest
             action expected to meet the SLO, and learns benefit and cost per
             action (remedy.py)
    safety   lowering-only actions, revert on worse, safe static fallback when
             the stall stays unexplained

This module is pure: `step` returns platform-neutral operations that a
harness maps onto Windows (native.py) or Linux (posix.py) controls.
    ("hint", app, "priority"|"io_priority", on)   blame's nudge or release
    ("level", app, action)                        set the culprit's remedy: priority | eco | capNN
    ("clear", app)                                remove eco and caps from an app
    ("fallback", on)                              priority hint on every managed job
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any

from .blame import Blamer, FactorialInterrogator
from .controller import Controller
from .remedy import LADDER, Optimizer

Op = tuple


@dataclass
class Shield:
    blamer: Blamer
    interrogator: FactorialInterrogator
    optimizer: Optimizer = field(default_factory=Optimizer)
    level: dict[int, str] = field(default_factory=dict)
    fallback: bool = False
    fallback_episodes: int = 0

    def step(self, controller: Controller, resource: str, names: dict[int, str]) -> list[Op]:
        ops: list[Op] = []
        ranked = self.blamer.rank(resource) if resource in ("cpu", "mem", "io") else []
        for d in self.interrogator.step(controller.engaged, controller.resource, controller.wsi_history, ranked, names):
            ops.append(("hint", d.app, d.hint, d.kind == "nudge"))
        confirmed = sorted(self.interrogator.confirmed)
        usage = {app: (self.blamer.cpu[-1].get(app, 0.0) if self.blamer.cpu else 0.0) for app in confirmed}
        recent = statistics.fmean(controller.wsi_history[-5:]) if controller.wsi_history else 0.0
        inconclusive = controller.engaged and not confirmed and self.interrogator.experiment is None
        for r in self.optimizer.step(recent, controller.resource, confirmed, usage, inconclusive):
            if r.kind == "apply":
                self.level[r.app] = r.action; ops.append(("level", r.app, r.action))
            elif r.kind == "revert":
                if self.level.pop(r.app, "priority") != "priority": ops.append(("clear", r.app))
            elif r.kind in ("fallback", "fallback_revert"):
                self.fallback = r.kind == "fallback"; self.fallback_episodes += self.fallback; ops.append(("fallback", self.fallback))
        return ops

    def summary(self) -> dict[str, Any]:
        return {"knowledge": self.optimizer.knowledge, "remedy_log": self.optimizer.log, "verdicts": self.interrogator.verdicts,
                "final_levels": dict(self.level), "fallback_episodes": self.fallback_episodes}


def ladder_for(platform: str) -> tuple[str, ...]:
    """EcoQoS exists only on Windows 11; elsewhere the ladder skips it."""
    return LADDER if platform == "windows" else tuple(a for a in LADDER if a != "eco")
