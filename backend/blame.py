"""Culprit blame: which application is stealing the foreground's time?

The controller answers *what* is contended (CPU, memory bandwidth, storage).
This module answers *who*, for arbitrary third-party processes, still without
privileges, drivers, ETW, or hardware counters. It is pure decision logic so it
can be unit-tested and replayed offline; `govern.py` executes its decisions.

Applications, not processes
    Real background work is a process tree (a build spawns compilers, a sync
    client spawns helpers). Processes are grouped by walking up the parent
    chain until a shell or system process (explorer.exe, services.exe,
    svchost.exe, a terminal host) or a configured stop PID is reached. That
    ancestor is the application root; usage is summed over its tree.

Stage 1, passive ranking (every control period)
    For the attributed resource r, each application's matched usage x_t (CPU
    cores for cpu and mem, I/O MB/s for io) is compared with the canary's
    excess on r, y_t, over a sliding horizon:
        share_hot = app's share of all matched usage during stalled periods
        rho       = Spearman(x, y)          (0 when either series is flat)
        score     = share_hot * (1 + rho)   (clipped at 0)
    A heavy consumer whose usage *tracks* the stall scores up to 2x its share.
    One whose usage is anti-correlated with the stall (e.g. it already runs
    at IDLE priority and only gets the CPU when the machine is calm) scores
    close to 0 despite being the top CPU user in Task Manager.

Stage 2, interventional confirmation (one suspect at a time)
    Correlation is not causation, and an always-on culprit has no variance to
    correlate. So the top suspect is *nudged* with the cheapest resource-
    matched hint (IDLE priority for cpu/mem, very-low I/O priority for io),
    and the canary decides: if mean WSI drops by `min_reduction` within
    `eval_periods`, the suspect is confirmed and stays nudged; otherwise it is
    exonerated, the nudge is reverted, and it is not retested for
    `cooldown_periods`. While the stall persists, the next suspect is tested.
    Every nudge is lowering-only and is reverted when the stall clears.

Stage 2', factorial randomized micro-experiments (mode "factorial")
    The one-at-a-time test is fooled when the load itself changes during the
    test window (a phased culprit going quiet makes an innocent suspect look
    guilty). The factorial mode runs a small randomized controlled trial on
    the live desktop instead: the top k suspects are each nudged in a random,
    balanced half of `slots` short slots, independently of one another (a 2^k
    factorial design). Because assignment is random, it is independent of
    whatever the load is doing, so the difference in mean WSI between a
    suspect's nudged and un-nudged slots is an unbiased estimate of its causal
    effect; a permutation test over the assignment gives its p-value. All k
    suspects are tested in the same few seconds. Suspects with a significant
    effect are kept nudged; the rest are released. The effect, in WSI points,
    is also an "interference bill": 1 point = 10 ms of foreground time lost
    per second.

Modes (for evaluation; the actuator is identical in all of them)
    top_cpu         Task-Manager heuristic: nudge the top CPU application.
    passive         nudge the top passive suspect, no confirmation.
    interventional  passive ranking + one-at-a-time causal confirmation.
    factorial       passive ranking + randomized factorial confirmation (SmartSwap).
"""
from __future__ import annotations

import math
import random
import statistics
from dataclasses import dataclass, field
from typing import Any

from .controller import HINTS, RESOURCES
from .stats import rank

# Processes that end the walk up the tree: they host many unrelated applications.
STOP_NAMES = frozenset({
    "system", "system idle process", "registry", "smss.exe", "csrss.exe", "wininit.exe", "winlogon.exe", "services.exe",
    "lsass.exe", "svchost.exe", "explorer.exe", "userinit.exe", "sihost.exe", "taskhostw.exe", "windowsterminal.exe",
    "openconsole.exe", "conhost.exe", "runtimebroker.exe", "applicationframehost.exe", "startmenuexperiencehost.exe",
})
# Processes SmartSwap never governs, whatever the blame says.
PROTECTED_NAMES = frozenset({
    "system", "system idle process", "registry", "memory compression", "memcompression", "smss.exe", "csrss.exe",
    "wininit.exe", "winlogon.exe", "services.exe", "lsass.exe", "lsaiso.exe", "dwm.exe", "fontdrvhost.exe",
    "audiodg.exe", "explorer.exe", "sihost.exe", "ctfmon.exe", "msmpeng.exe", "securityhealthservice.exe",
})
MODES = ("top_cpu", "passive", "interventional", "factorial")
CALM_SNAPSHOT_EVERY = 5  # control periods between process snapshots while no stall is engaged (1 Hz at 200 ms)


@dataclass(frozen=True)
class ProcSample:
    """One process at one instant; cumulative counters."""

    pid: int
    ppid: int
    name: str
    cpu_s: float
    io_bytes: float
    create_time: float = 0.0
    same_user: bool = True


def app_roots(procs: list[ProcSample], stop_pids: frozenset[int] = frozenset()) -> dict[int, int]:
    """Map every pid to the root pid of its application tree.

    The walk stops below a STOP_NAMES process or a stop pid, and at a parent
    that started after its child (the parent pid was reused).
    """
    by_pid = {p.pid: p for p in procs}; roots: dict[int, int] = {}
    for p in procs:
        node, seen = p, {p.pid}
        while True:
            parent = by_pid.get(node.ppid)
            if parent is None or parent.pid in seen or parent.pid in stop_pids or parent.name.lower() in STOP_NAMES: break
            if parent.create_time and node.create_time and parent.create_time > node.create_time: break
            node = parent; seen.add(node.pid)
        roots[p.pid] = node.pid
    return roots


def _spearman(x: list[float], y: list[float]) -> float:
    if len(x) < 3 or len(set(x)) < 2 or len(set(y)) < 2: return 0.0
    rx, ry = rank(x), rank(y); mx, my = statistics.fmean(rx), statistics.fmean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
    return num / den if den else 0.0


@dataclass
class Suspect:
    app: int            # root pid
    name: str
    pids: list[int]
    score: float
    share_hot: float
    rho: float
    usage: float        # mean matched usage over the horizon (cores or MB/s)
    cpu_cores: float    # mean CPU cores over the horizon
    protected: bool

    def as_dict(self) -> dict[str, Any]:
        return {"app": self.app, "name": self.name, "pids": len(self.pids), "score": round(self.score, 4), "share_hot": round(self.share_hot, 4),
                "rho": round(self.rho, 3), "usage": round(self.usage, 3), "cpu_cores": round(self.cpu_cores, 3), "protected": self.protected}


@dataclass
class Blamer:
    """Keeps per-application usage aligned with the canary trace and ranks suspects."""

    period_s: float
    horizon: int = 50                   # periods (10 s at the 200 ms control period)
    engage_wsi: float = 5.0
    stop_pids: frozenset[int] = frozenset()
    exclude_pids: frozenset[int] = frozenset()   # SmartSwap, its canary, its probes
    last: dict[int, ProcSample] = field(default_factory=dict)
    names: dict[int, str] = field(default_factory=dict)
    members: dict[int, list[int]] = field(default_factory=dict)
    protected: dict[int, bool] = field(default_factory=dict)
    cpu: list[dict[int, float]] = field(default_factory=list)   # per period: app -> cores
    io: list[dict[int, float]] = field(default_factory=list)    # per period: app -> MB/s
    signal: list[dict[str, float]] = field(default_factory=list)  # per period: wsi + excess per resource

    def update(self, snapshot: list[ProcSample], wsi: float, excess: dict[str, float], foreground: int | None = None, elapsed_s: float | None = None) -> None:
        """Add one observation. `elapsed_s` is the time since the previous snapshot (default: one control period),
        so callers may snapshot at a lower rate while the desktop is calm."""
        dt = elapsed_s or self.period_s
        roots = app_roots(snapshot, self.stop_pids)
        fg_root = roots.get(foreground) if foreground else None
        cpu: dict[int, float] = {}; io: dict[int, float] = {}; members: dict[int, list[int]] = {}
        for p in snapshot:
            prev = self.last.get(p.pid)
            if p.pid in self.exclude_pids or prev is None or prev.create_time != p.create_time: continue
            root = roots[p.pid]
            if root in self.exclude_pids: continue
            cpu[root] = cpu.get(root, 0.0) + max(0.0, p.cpu_s - prev.cpu_s) / dt
            io[root] = io.get(root, 0.0) + max(0.0, p.io_bytes - prev.io_bytes) / 1e6 / dt
            members.setdefault(root, []).append(p.pid)
        by_pid = {p.pid: p for p in snapshot}
        for root, pids in members.items():
            self.members[root] = pids
            self.names[root] = by_pid[root].name if root in by_pid else self.names.get(root, "?")
            self.protected[root] = (root == fg_root or any(by_pid[q].name.lower() in PROTECTED_NAMES or not by_pid[q].same_user for q in pids if q in by_pid))
        self.last = by_pid
        self.cpu.append(cpu); self.io.append(io); self.signal.append({"wsi": wsi, **{r: excess.get(r, 0.0) for r in RESOURCES}})
        for series in (self.cpu, self.io, self.signal): del series[:-self.horizon]

    def rank(self, resource: str) -> list[Suspect]:
        """Suspects for `resource`, best first."""
        if resource not in RESOURCES or not self.signal: return []
        usage = self.io if resource == "io" else self.cpu
        apps = {a for period in usage for a in period}
        hot = [i for i, s in enumerate(self.signal) if s["wsi"] >= self.engage_wsi]
        hot_total = sum(sum(usage[i].values()) for i in hot)
        y = [s[resource] for s in self.signal]
        out = []
        for app in apps:
            x = [period.get(app, 0.0) for period in usage]
            share_hot = sum(usage[i].get(app, 0.0) for i in hot) / hot_total if hot_total > 0 else 0.0
            rho = _spearman(x, y)
            out.append(Suspect(app, self.names.get(app, "?"), self.members.get(app, []), max(0.0, share_hot * (1 + rho)), share_hot, rho,
                               statistics.fmean(x), statistics.fmean(period.get(app, 0.0) for period in self.cpu), self.protected.get(app, False)))
        return sorted(out, key=lambda s: (-s.score, -s.usage, s.app))

    def top_cpu(self, window: int = 10) -> list[Suspect]:
        """Task-Manager heuristic: applications by recent CPU use, ignoring the stall signal."""
        recent = self.cpu[-window:]; apps = {a for period in recent for a in period}
        out = [Suspect(a, self.names.get(a, "?"), self.members.get(a, []), statistics.fmean(p.get(a, 0.0) for p in recent), 0.0, 0.0,
                       statistics.fmean(p.get(a, 0.0) for p in recent), statistics.fmean(p.get(a, 0.0) for p in recent), self.protected.get(a, False)) for a in apps]
        return sorted(out, key=lambda s: (-s.score, s.app))


@dataclass
class BlameDecision:
    kind: str        # nudge | release
    app: int
    hint: str        # priority | io_priority
    reason: str


@dataclass
class Interrogator:
    """Turns suspect rankings into nudges, confirming causality by intervention."""

    mode: str = "interventional"
    slo_wsi: float = 3.0
    eval_periods: int = 8
    min_reduction: float = 0.3
    cooldown_periods: int = 50
    min_score: float = 0.05
    period: int = 0
    nudged: dict[int, str] = field(default_factory=dict)        # app -> hint currently applied
    testing: list[Any] | None = None                              # [app, periods, baseline WSI]
    confirmed: set[int] = field(default_factory=set)
    exonerated: dict[int, int] = field(default_factory=dict)      # app -> period it may be retested
    verdicts: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.mode not in MODES: raise ValueError(f"unknown blame mode {self.mode}")

    def step(self, engaged: bool, resource: str, wsi_history: list[float], ranked: list[Suspect], names: dict[int, str] | None = None) -> list[BlameDecision]:
        self.period += 1; out: list[BlameDecision] = []; names = names or {}
        recent = statistics.fmean(wsi_history[-5:]) if wsi_history else 0.0
        if not engaged:
            if self.nudged:  # the stall cleared: give everything back
                out += [BlameDecision("release", app, hint, "stall cleared") for app, hint in sorted(self.nudged.items())]
                self.nudged.clear(); self.testing = None; self.confirmed.clear()
            return out
        if self.testing is not None:
            self.testing[1] += 1
            app, periods, baseline = self.testing
            if periods >= self.eval_periods and self.mode != "interventional":
                self.testing = None  # baselines keep every nudge; they only pace themselves like the tester
            elif periods >= self.eval_periods:
                self.testing = None; helped = baseline > 0 and recent <= (1 - self.min_reduction) * baseline
                self.verdicts.append({"period": self.period, "app": app, "name": names.get(app, "?"), "verdict": "confirmed" if helped else "exonerated", "wsi_before": round(baseline, 3), "wsi_after": round(recent, 3)})
                if helped: self.confirmed.add(app)
                else:
                    self.exonerated[app] = self.period + self.cooldown_periods
                    if app in self.nudged: out.append(BlameDecision("release", app, self.nudged.pop(app), f"exonerated: mean WSI {baseline:.1f}% -> {recent:.1f}%"))
            return out
        if recent <= self.slo_wsi or resource not in HINTS: return out
        for s in ranked:
            if s.protected or s.app in self.nudged or self.exonerated.get(s.app, -1) > self.period: continue
            if self.mode != "top_cpu" and s.score < self.min_score: break
            if s.usage <= 0: continue
            hint = HINTS[resource]; self.nudged[s.app] = hint
            self.testing = [s.app, 0, recent]
            if self.mode == "interventional":
                out.append(BlameDecision("nudge", s.app, hint, f"testing suspect {s.name} (score {s.score:.2f}, share {s.share_hot:.0%}, rho {s.rho:+.2f}); mean WSI {recent:.1f}%"))
            else:
                self.verdicts.append({"period": self.period, "app": s.app, "name": s.name, "verdict": "blamed", "wsi_before": round(recent, 3), "wsi_after": None})
                out.append(BlameDecision("nudge", s.app, hint, f"{self.mode}: blamed {s.name} (score {s.score:.2f})"))
            break
        return out


def permutation_effect(y: list[float], x: list[int], permutations: int = 2000, seed: int = 5) -> tuple[float, float]:
    """Mean(y | x=1) - mean(y | x=0) and its one-sided permutation p-value for a *reduction*."""
    on = [a for a, b in zip(y, x) if b]; off = [a for a, b in zip(y, x) if not b]
    if not on or not off: return 0.0, 1.0
    effect = statistics.fmean(on) - statistics.fmean(off)
    rng = random.Random(seed); shuffled = list(x); hits = 0
    for _ in range(permutations):
        rng.shuffle(shuffled)
        a = [v for v, b in zip(y, shuffled) if b]; c = [v for v, b in zip(y, shuffled) if not b]
        if statistics.fmean(a) - statistics.fmean(c) <= effect + 1e-12: hits += 1
    return effect, (hits + 1) / (permutations + 1)


@dataclass
class FactorialInterrogator:
    """Randomized factorial micro-experiments over the top-k suspects (see module docstring)."""

    mode: str = "factorial"
    slo_wsi: float = 3.0
    k: int = 3
    slots: int = 8                  # balanced: each suspect is nudged in exactly slots/2 of them
    slot_periods: int = 3           # the first period of a slot lets the canary window refill
    alpha: float = 0.05
    min_effect: float = 1.0         # WSI points; smaller effects are not worth a nudge
    max_rounds: int = 3             # inconclusive suspects get up to this many randomized rounds, pooled
    inconclusive_p: float = 0.3     # below this (with a promising effect) a suspect earns another round
    cooldown_periods: int = 50
    min_score: float = 0.02
    seed: int = 0
    # Presumed guilty: when a stall starts, nudge every candidate at once (immediate protection), hold
    # `hold_periods`, then run the same randomized experiments as *release* tests. A job whose release
    # brings the stall back stays nudged; one whose release changes nothing is restored for good; one
    # still inconclusive after max_rounds stays nudged (the safe side). Candidates are every managed
    # job, not the passive top-k, so a mislabelled stall cannot hide the culprit.
    presume_guilty: bool = False
    hold_periods: int = 5
    period: int = 0
    nudged: dict[int, str] = field(default_factory=dict)
    held: set[int] = field(default_factory=set)
    holding: list[Any] | None = None
    confirmed: set[int] = field(default_factory=set)
    exonerated: dict[int, int] = field(default_factory=dict)
    verdicts: list[dict[str, Any]] = field(default_factory=list)
    experiment: dict[str, Any] | None = None

    def _set(self, app: int, hint: str, on: bool, reason: str) -> list[BlameDecision]:
        if on and app not in self.nudged: self.nudged[app] = hint; return [BlameDecision("nudge", app, hint, reason)]
        if not on and app in self.nudged and app not in self.confirmed: return [BlameDecision("release", app, self.nudged.pop(app), reason)]
        return []

    def step(self, engaged: bool, resource: str, wsi_history: list[float], ranked: list[Suspect], names: dict[int, str] | None = None) -> list[BlameDecision]:
        self.period += 1; out: list[BlameDecision] = []; names = names or {}
        if not engaged:
            if self.nudged:
                out += [BlameDecision("release", app, hint, "stall cleared") for app, hint in sorted(self.nudged.items())]
                self.nudged.clear(); self.confirmed.clear(); self.held.clear()
            self.experiment = None; self.holding = None
            return out
        if self.holding is not None:  # presumed guilty: protection first, then the release experiment
            self.holding[0] -= 1
            if self.holding[0] <= 0:
                _, apps, hint, base = self.holding; self.holding = None
                return out + self._start(apps, hint, base, names, 1, {})
            return out
        e = self.experiment
        if e is not None:
            e["tick"] += 1; slot, pos = divmod(e["tick"], self.slot_periods)
            if pos != 0 and wsi_history: e["y"][slot].append(wsi_history[-1])  # skip the settling period
            if pos == 0 and slot < self.slots:  # next slot: switch every suspect to its assignment
                for app, plan in e["plan"].items(): out += self._set(app, e["hint"], bool(plan[slot]), f"micro-experiment slot {slot + 1}/{self.slots}")
            if slot >= self.slots:
                self.experiment = None; look_alpha = self.alpha / self.max_rounds  # Bonferroni over the planned looks
                y = [statistics.fmean(v) if v else 0.0 for v in e["y"]]; again = []
                for app, plan in e["plan"].items():
                    ys, xs = e["hist"].setdefault(app, ([], [])); ys += y; xs += plan
                    effect, p = permutation_effect(ys, xs, seed=self.seed + self.period)
                    guilty = p < look_alpha and -effect >= self.min_effect
                    if not guilty and e["round"] < self.max_rounds and p < self.inconclusive_p and -effect >= self.min_effect / 2:
                        again.append(app); continue  # promising but not yet significant: pool another round
                    self.verdicts.append({"period": self.period, "app": app, "name": names.get(app, "?"), "verdict": "confirmed" if guilty else "exonerated",
                                          "effect_wsi": round(effect, 3), "p": round(p, 4), "rounds": e["round"], "bill_ms_per_s": round(max(0.0, -effect) * 10, 1),
                                          "wsi_before": round(e["baseline"], 3), "wsi_after": round(statistics.fmean(y), 3)})
                    if guilty:
                        if app not in self.nudged: out.append(BlameDecision("nudge", app, e["hint"], f"confirmed: nudging cut WSI by {-effect:.1f} points (p={p:.3f}, {e['round']} rounds)")); self.nudged[app] = e["hint"]
                        self.confirmed.add(app)
                    elif self.presume_guilty and p < self.inconclusive_p and -effect >= self.min_effect / 2:
                        self.verdicts[-1]["verdict"] = "held"; self.held.add(app)  # not proven innocent: stay on the safe side
                        if app not in self.nudged: out.append(BlameDecision("nudge", app, e["hint"], f"inconclusive after {e['round']} rounds: kept lowered")); self.nudged[app] = e["hint"]
                    else:
                        self.exonerated[app] = self.period + self.cooldown_periods
                        if app in self.nudged: out.append(BlameDecision("release", app, self.nudged.pop(app), f"exonerated: effect {effect:+.1f} WSI points (p={p:.2f})"))
                if again: out += self._start(again, e["hint"], e["baseline"], names, e["round"] + 1, e["hist"])
            return out
        recent = statistics.fmean(wsi_history[-5:]) if wsi_history else 0.0
        if recent <= self.slo_wsi or resource not in HINTS: return out
        eligible = [s for s in ranked if not s.protected and s.app not in self.confirmed and s.app not in self.held and self.exonerated.get(s.app, -1) <= self.period and s.usage > 0]
        if self.presume_guilty:
            pool = eligible[: max(self.k, 6)]
            if not pool: return out
            for x in pool: out += self._set(x.app, HINTS[resource], True, f"presumed guilty while the stall is investigated ({len(pool)} jobs lowered)")
            self.holding = [self.hold_periods, [x.app for x in pool], HINTS[resource], recent]
            return out
        pool = [s for s in eligible if s.score >= self.min_score][: self.k]
        if not pool: return out
        return out + self._start([x.app for x in pool], HINTS[resource], recent, names, 1, {})

    def _start(self, apps: list[int], hint: str, baseline: float, names: dict[int, str], rnd: int, hist: dict[int, Any]) -> list[BlameDecision]:
        """Begin one randomized round: each suspect nudged in a fresh random, balanced half of the slots."""
        rng = random.Random(self.seed * 7919 + self.period * 31 + rnd); plan = {}
        for app in apps:
            assignment = [1] * (self.slots // 2) + [0] * (self.slots - self.slots // 2); rng.shuffle(assignment); plan[app] = assignment
        self.experiment = {"plan": plan, "hint": hint, "tick": 0, "y": [[] for _ in range(self.slots)], "baseline": baseline, "round": rnd, "hist": hist}
        out: list[BlameDecision] = []
        for app, a in plan.items(): out += self._set(app, hint, bool(a[0]), f"micro-experiment round {rnd} over {len(plan)} suspects ({', '.join(names.get(x, '?') for x in plan)}), slot 1/{self.slots}")
        return out


def make_interrogator(mode: str, **kw: Any) -> Interrogator | FactorialInterrogator:
    """The confirmation stage for a blame mode, configured from the controller's thresholds."""
    if mode == "factorial":
        return FactorialInterrogator(slo_wsi=kw.get("slo_wsi", 3.0), seed=kw.get("seed", 0))
    return Interrogator(mode, slo_wsi=kw.get("slo_wsi", 3.0), eval_periods=kw.get("eval_periods", 8), min_reduction=kw.get("min_reduction", 0.3))
