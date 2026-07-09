#  Project:      dfe-engine
#  File:         tests/hunt_runner/test_spread_distribution.py
#  Purpose:      Synthetic spread test - same-interval hunts fan evenly (no herd)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The load-spread is deterministic (hash(hunt_id) mod window), so anti-thundering-
herd is a fast statistical property, not a slow timing test: over a fleet of hunts
on one interval the fire offsets must fan evenly across the window."""

from __future__ import annotations

from dfe_engine.hunt_runner.spread import current_fire, phase_offset


def test_offsets_fan_evenly_across_the_window():
    interval, fraction, n = 600, 0.8, 200
    window = int(interval * fraction)  # 480
    offsets = [phase_offset(f"hunt-{i}", interval, fraction) for i in range(n)]

    assert all(0 <= o < window for o in offsets)

    # Bucket into 10 bins. A thundering herd would spike one bin; an even fan keeps
    # every bin near the mean (n/bins = 20).
    bins = 10
    counts = [0] * bins
    for o in offsets:
        counts[min(bins - 1, o * bins // window)] += 1
    mean = n / bins
    assert max(counts) <= 2.0 * mean, f"herd spike: {counts}"
    assert min(counts) >= 0.3 * mean, f"coverage gap: {counts}"

    # the fan spans most of the window (not clustered in one corner)
    assert min(offsets) < window * 0.15
    assert max(offsets) > window * 0.85


def test_current_fire_is_stable_and_within_the_interval():
    now = 1_000_000
    boundary = (now // 600) * 600
    for i in range(50):
        h = f"hunt-{i}"
        fire = current_fire(h, 600, now)
        assert fire == current_fire(h, 600, now)  # deterministic
        assert boundary <= fire < boundary + int(600 * 0.8) + 1


def test_distinct_hunts_get_distinct_offsets():
    # 100 hunts should not all collide onto a handful of offsets
    offsets = {phase_offset(f"hunt-{i}", 600) for i in range(100)}
    assert len(offsets) > 60  # hash spread -> mostly distinct
