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

Modes (for evaluation; the actuator is identical in all of them)
    top_cpu         Task-Manager heuristic: nudge the top CPU application.
    passive         nudge the top passive suspect, no confirmation.
    interventional  passive ranking + causal confirmation (SmartSwap).
"""
from __future__ import annotations

import math
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
MODES = ("top_cpu", "passive", "interventional")


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

    def update(self, snapshot: list[ProcSample], wsi: float, excess: dict[str, float], foreground: int | None = None) -> None:
        roots = app_roots(snapshot, self.stop_pids)
        fg_root = roots.get(foreground) if foreground else None
        cpu: dict[int, float] = {}; io: dict[int, float] = {}; members: dict[int, list[int]] = {}
        for p in snapshot:
            prev = self.last.get(p.pid)
            if p.pid in self.exclude_pids or prev is None or prev.create_time != p.create_time: continue
            root = roots[p.pid]
            if root in self.exclude_pids: continue
            cpu[root] = cpu.get(root, 0.0) + max(0.0, p.cpu_s - prev.cpu_s) / self.period_s
            io[root] = io.get(root, 0.0) + max(0.0, p.io_bytes - prev.io_bytes) / 1e6 / self.period_s
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
