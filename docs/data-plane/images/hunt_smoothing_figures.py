#!/usr/bin/env python3
#  Project:      dfe-engine
#  File:         docs/data-plane/images/hunt_smoothing_figures.py
#  Purpose:      Draw the figures for docs/data-plane/hunt-schedule-smoothing.md
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Draw the hunt schedule smoothing figures as SVG, beside this script.

The curves are illustrative shapes, not measurements. Each SVG has a transparent
background and mid-grey ink, so it reads on the docs site's light and dark themes.

Usage::

    uv run --with matplotlib python docs/data-plane/images/hunt_smoothing_figures.py
"""

from pathlib import Path

# matplotlib comes from `uv run --with`, not a project dependency, so ty cannot resolve it.
import matplotlib as mpl  # ty: ignore

mpl.use("svg")
import matplotlib.pyplot as plt  # ty: ignore

HERE = Path(__file__).resolve().parent
INK = "#85847f"
BLUE = "#3987e5"
ORANGE = "#e3602b"
BUCKETS = 24

plt.rcParams.update(
    {
        "svg.fonttype": "none",
        "svg.hashsalt": "dfe-hunt-smoothing",
        "font.family": "sans-serif",
        "font.size": 11,
        "text.color": INK,
        "axes.edgecolor": INK,
        "axes.labelcolor": INK,
        "axes.titlecolor": INK,
        "xtick.color": INK,
        "ytick.color": INK,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "figure.facecolor": "none",
        "axes.facecolor": "none",
        "savefig.transparent": True,
    }
)


def _before() -> list[float]:
    """Pressure per bucket with three heavy hunts stacked on colliding offsets."""
    load = [1.0] * BUCKETS
    for peak, height in ((3, 9.0), (10, 6.0), (17, 4.0)):
        load[peak] += height
        load[peak - 1] += height / 4
        load[peak + 1] += height / 4
    return load


def _after(total: float) -> list[float]:
    """The same total pressure with the phases drifted apart, as a small zero-sum ripple."""
    mean = total / BUCKETS
    ripple = (0.18, -0.06, -0.12, 0.0)
    return [mean * (1 + ripple[b % len(ripple)]) for b in range(BUCKETS)]


def _bars(ax: plt.Axes, load: list[float], title: str) -> None:
    """One panel of the before-and-after pair, with its mean and peak/mean marked."""
    mean = sum(load) / len(load)
    ax.bar(range(len(load)), load, width=0.8, color=BLUE)
    ax.axhline(mean, color=INK, linestyle="--", linewidth=1)
    ax.text(len(load) - 0.6, mean, "mean", va="bottom", ha="right")
    ax.set_title(f"{title}\npeak/mean = {max(load) / mean:.1f}", loc="left")
    ax.set_xlabel("bucket b across one hyperperiod H")
    ax.set_xticks([])


def before_after() -> None:
    """Aggregate ClickHouse pressure P(b), before and after smoothing."""
    before = _before()
    after = _after(sum(before))
    fig, (left, right) = plt.subplots(1, 2, figsize=(10, 3.6), sharey=True)
    _bars(left, before, "Before: offsets collide")
    _bars(right, after, "After: phases drifted apart")
    left.set_ylabel("pressure P(b)")
    left.set_yticks([])
    fig.tight_layout()
    fig.savefig(HERE / "hunt-smoothing-before-after.svg", metadata={"Date": None})
    plt.close(fig)


def cpu_vs_memory() -> None:
    """CPU degrades on a slope, memory fails at a cliff."""
    load = [x / 100 for x in range(101)]
    limit = 0.7
    fig, (cpu, mem) = plt.subplots(1, 2, figsize=(10, 3.4))

    cpu.plot(load, [0.15 + 0.85 * x**2 for x in load], color=BLUE, linewidth=2)
    cpu.set_title("CPU: a soft slope, queries slow down", loc="left")
    cpu.set_ylabel("p95 latency")

    mem.plot(load, [1.0 if x < limit else 0.2 for x in load], color=ORANGE, linewidth=2)
    mem.axvline(limit, color=INK, linestyle="--", linewidth=1)
    mem.text(limit + 0.02, 0.62, "server memory limit")
    mem.text(limit + 0.02, 0.3, "code 241 fails\nmany queries at once")
    mem.set_title("Memory: a hard cliff, many fail at once", loc="left")
    mem.set_ylabel("query success")
    mem.set_ylim(0, 1.15)

    for ax in (cpu, mem):
        ax.set_xlabel("load")
        ax.set_xticks([])
        ax.set_yticks([])
    fig.tight_layout()
    fig.savefig(HERE / "hunt-smoothing-cpu-vs-memory.svg", metadata={"Date": None})
    plt.close(fig)


if __name__ == "__main__":
    before_after()
    cpu_vs_memory()
