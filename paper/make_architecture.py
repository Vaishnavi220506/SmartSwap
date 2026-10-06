"""Draws the SmartSwap v2 architecture figure (fig_architecture.pdf), full text width."""
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

INK, MUTED, LINE = "#0b0b0b", "#52514e", "#8a8984"
FILL = {"probe": "#e6f0fb", "ctl": "#e3f5ee", "act": "#fdeee7", "os": "#f1f0ec"}
plt.rcParams.update({"font.family": "serif"})
W, H = 7.0, 2.05
fig = plt.figure(figsize=(W, H)); ax = fig.add_axes([0, 0, 1, 1]); ax.set_xlim(0, 700); ax.set_ylim(0, 205); ax.axis("off")


def box(x, y, w, h, title, body, kind):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=2,rounding_size=6", fc=FILL[kind], ec=LINE, lw=0.6))
    ax.text(x + w / 2, y + h - 9, title, ha="center", va="top", fontsize=7.5, weight="bold", color=INK)
    ax.text(x + w / 2, y + h - 27, body, ha="center", va="top", fontsize=6.3, color=MUTED, linespacing=1.3)


def arrow(p, q, label="", above=True, color=INK, dashed=False, rad=0.0):
    ax.add_patch(FancyArrowPatch(p, q, arrowstyle="-|>", mutation_scale=8, lw=0.75, color=color, linestyle=(0, (3, 2)) if dashed else "-", connectionstyle=f"arc3,rad={rad}"))
    if label: ax.text((p[0] + q[0]) / 2, (p[1] + q[1]) / 2 + (5 if above else -5), label, ha="center", va="bottom" if above else "top", fontsize=6, color=MUTED)


y, h, w = 92, 100, 104  # arrows between boxes are unlabeled; box titles name each stage
xs = [8, 146, 284, 422, 560]
box(xs[0], y, w, h, "Canary", "wakes every 40 ms;\ntimes sched delay,\ncompute, 8 MiB\nstream, 4 KiB\nunbuffered read", "probe")
box(xs[1], y, w, h, "WSI + attribution", "share of wall time\nlost to stalls;\nspecificity order\ncpu > mem > io", "ctl")
box(xs[2], y, w, h, "Controller", "gate + hysteresis;\nladder hint -> cap;\nAIMD on the cap;\nbenefit check;\nload-aware release", "ctl")
box(xs[3], y, w, h, "Actuator", "Win32/NT calls:\npriority class,\nI/O priority,\nJob CPU-rate cap;\nread-back verify", "act")
box(xs[4], y, w + 26, h, "Governed group", "background processes\nSmartSwap started or\nwas told to manage;\nevery action is\nreverted and verified", "os")
for a, b, label in ((0, 1, ""), (1, 2, ""), (2, 3, ""), (3, 4, "")):
    arrow((xs[a] + w + 3, y + h / 2), (xs[b] - 3, y + h / 2), label)
arrow((xs[4] + 20, y - 3), (xs[2] + w / 2 + 10, y - 3), "group CPU / I/O activity (release only when quiet)", above=False, color=LINE, dashed=True, rad=0.0)
box(8, 6, 682, 46, "Interactive foreground", "not modified and not observed by the controller; shares cores, LLC / memory bandwidth and the storage queue with the governed group", "probe")
ax.annotate("", xy=(610, 54), xytext=(610, 88), arrowprops=dict(arrowstyle="<|-|>", color=LINE, lw=0.7, linestyle=(0, (3, 2)), mutation_scale=8))
ax.text(616, 71, "interference", fontsize=6, color=MUTED, va="center")
ax.annotate("", xy=(60, 54), xytext=(60, 88), arrowprops=dict(arrowstyle="<|-|>", color=LINE, lw=0.7, linestyle=(0, (3, 2)), mutation_scale=8))
ax.text(66, 71, "feels the same contention", fontsize=6, color=MUTED, va="center")
out = Path(__file__).resolve().parent / "figures"; out.mkdir(exist_ok=True)
fig.savefig(out / "fig_architecture.pdf"); fig.savefig(out / "fig_architecture.png", dpi=300)
