#  Project:      dfe-engine
#  File:         tests/unit/test_hunts/test_smoothing_model.py
#  Purpose:      The smoothing maths holds its invariants on random and adversarial fleets
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Properties of the phase smoother's maths, no ClickHouse.

The claims the design makes are checked as properties over seeded random fleets:
the curve totals never change when an offset moves, the incremental objective a
move is scored on equals a full recompute, every accepted move lowers ``J`` by
the hysteresis margin and stays inside its bound and step, and a colliding fleet
flattens. The failure rows sit beside the valid ones: hysteresis holds a tiny
gain, cool-down holds a moved hunt, the memory cap holds a move that would stack
memory, and an empty or zero-cost fleet moves nothing.
"""

import itertools
import random
from collections.abc import Sequence

import numpy as np
import pytest

from dfe_engine.hunts.smoothing_model import (
    Cost,
    Move,
    RateHunt,
    bucket_count,
    covered_buckets,
    flatness,
    hyperperiod,
    improves,
    load_curves,
    objective,
    objective_floor,
    pressure,
    propose_move,
    pulse_width,
    smoothed,
    variances,
)

BUCKET = 30
HOUR = 3600


def apply_move(hunts: Sequence[RateHunt], move: Move, now: int) -> list[RateHunt]:
    """The fleet with *move* applied, the moved hunt stamped at *now*."""
    moved = []
    for hunt in hunts:
        if hunt.hunt_id == move.hunt_id:
            hunt = RateHunt(
                hunt_id=hunt.hunt_id,
                interval=hunt.interval,
                offset=move.new_offset,
                bound=hunt.bound,
                cost=hunt.cost,
                moved_at=now,
            )
        moved.append(hunt)
    return moved


def _hunt(
    hunt_id: str,
    interval: int = 600,
    offset: int = 0,
    *,
    mem: float = 1e9,
    duration_ms: float = 20_000,
    cpu_us: float | None = None,
    moved_at: int | None = None,
) -> RateHunt:
    return RateHunt(
        hunt_id=hunt_id,
        interval=interval,
        offset=offset,
        bound=int(interval * 0.8),
        cost=Cost(mem_bytes=mem, duration_ms=duration_ms, cpu_us=cpu_us, fires=10),
        moved_at=moved_at,
    )


def _random_fleet(seed: int, size: int = 12) -> list[RateHunt]:
    rng = random.Random(seed)
    intervals = [300, 600, 900, 1800, 3600]
    hunts = []
    for i in range(size):
        interval = rng.choice(intervals)
        hunts.append(
            _hunt(
                f"h{i}",
                interval,
                rng.randrange(int(interval * 0.8)),
                mem=rng.uniform(1e8, 4e9),
                duration_ms=rng.uniform(500, 90_000),
            )
        )
    return hunts


def _propose(
    hunts: list[RateHunt],
    *,
    weight_memory: float = 0.5,
    hysteresis: float = 0.01,
    memory_cap: float | None = None,
    now: int = 1_800_000_000,
    cooldown_seconds: float = 600,
) -> Move | None:
    return propose_move(
        hunts,
        bucket=BUCKET,
        horizon=HOUR,
        weight_memory=weight_memory,
        max_step_fraction=0.1,
        hysteresis=hysteresis,
        memory_cap=memory_cap,
        now=now,
        cooldown_seconds=cooldown_seconds,
    )


@pytest.mark.parametrize(
    ("intervals", "cap", "want"),
    [
        pytest.param([300, 900, 3600], 86400, (3600, False), id="harmonic"),
        pytest.param([300, 420], 86400, (2100, False), id="coprime_lcm"),
        pytest.param([3600, 7 * 3600, 11 * 3600], 86400, (86400, True), id="capped"),
        pytest.param([], 86400, (1, False), id="empty"),
        pytest.param([0], 86400, (1, False), id="zero_interval_is_one"),
    ],
)
def test_hyperperiod_is_the_lcm_held_to_the_cap(intervals, cap, want):
    assert hyperperiod(intervals, cap) == want


@pytest.mark.parametrize(
    ("duration_ms", "want"),
    [
        pytest.param(0, 1, id="zero_is_one_bucket"),
        pytest.param(30_000, 1, id="exactly_one_bucket"),
        pytest.param(30_001, 2, id="just_over_rounds_up"),
        pytest.param(150_000, 5, id="five_buckets"),
    ],
)
def test_pulse_width_rounds_up_to_at_least_one_bucket(duration_ms, want):
    assert pulse_width(duration_ms, BUCKET) == want


def test_covered_buckets_wrap_the_hyperperiod_and_count_overlap_twice():
    # A 600s hunt at offset 570 with a 60s pulse covers buckets 19,20 then 39,0 ...
    hunt = _hunt("h", 600, 570, duration_ms=60_000)
    covered = covered_buckets(hunt, 570, bucket=BUCKET, horizon=HOUR)
    assert len(covered) == 6 * 2
    assert bucket_count(HOUR, BUCKET) == 120
    assert set(covered.tolist()) >= {19, 20, 119, 0}
    # A hunt running longer than its interval loads itself: 600s interval, 1200s pulse.
    long = _hunt("l", 600, 0, duration_ms=1_200_000)
    counts = np.bincount(covered_buckets(long, 0, bucket=BUCKET, horizon=HOUR), minlength=120)
    assert counts.min() == 2
    assert counts.max() == 2


@pytest.mark.parametrize("seed", range(6))
def test_curve_totals_are_invariant_under_any_offset_move(seed):
    hunts = _random_fleet(seed)
    before = load_curves(hunts, bucket=BUCKET, horizon=HOUR)
    rng = random.Random(seed)
    target = rng.choice(hunts)
    move = Move(target.hunt_id, target.offset, rng.randrange(target.bound), 0.0, 0.0)
    after = load_curves(apply_move(hunts, move, 0), bucket=BUCKET, horizon=HOUR)
    assert after.mem.sum() == pytest.approx(before.mem.sum())
    assert after.cpu.sum() == pytest.approx(before.cpu.sum())
    assert pressure(after, 0.5).sum() == pytest.approx(pressure(before, 0.5).sum())


@pytest.mark.parametrize("seed", range(6))
def test_objective_is_variance_up_to_the_constant_total(seed):
    p = pressure(load_curves(_random_fleet(seed), bucket=BUCKET, horizon=HOUR), 0.5)
    total, buckets = p.sum(), p.size
    assert objective(p) == pytest.approx(total**2 / buckets + buckets * p.var())


@pytest.mark.parametrize("seed", range(8))
def test_the_incremental_score_equals_a_full_recompute_and_respects_the_bounds(seed):
    hunts = _random_fleet(seed)
    move = _propose(hunts, hysteresis=0.0)
    if move is None:
        pytest.skip("this fleet is already at a local minimum")
    before = objective(pressure(load_curves(hunts, bucket=BUCKET, horizon=HOUR), 0.5))
    after_fleet = apply_move(hunts, move, 0)
    after = objective(pressure(load_curves(after_fleet, bucket=BUCKET, horizon=HOUR), 0.5))
    assert move.objective_before == pytest.approx(before)
    assert move.objective_after == pytest.approx(after)
    assert after <= before
    scores_before = variances(pressure(load_curves(hunts, bucket=BUCKET, horizon=HOUR), 0.5))
    scores_after = variances(pressure(load_curves(after_fleet, bucket=BUCKET, horizon=HOUR), 0.5))
    assert improves(scores_before, scores_after, 0.0)
    moved = next(h for h in hunts if h.hunt_id == move.hunt_id)
    assert 0 <= move.new_offset < moved.bound
    assert abs(move.new_offset - move.old_offset) <= int(moved.interval * 0.1)
    assert (move.new_offset - move.old_offset) % BUCKET == 0


def test_a_colliding_fleet_flattens_and_j_never_rises():
    hunts = [_hunt(f"h{i}", 600, 0, duration_ms=30_000) for i in range(10)]
    now = 1_800_000_000
    start = flatness(pressure(load_curves(hunts, bucket=BUCKET, horizon=HOUR), 0.5))
    assert start == pytest.approx(20.0)  # all ten stack into one of 20 buckets per interval
    objectives = [objective(pressure(load_curves(hunts, bucket=BUCKET, horizon=HOUR), 0.5))]
    moves = 0
    for cycle in range(80):
        move = _propose(hunts, now=now + cycle * 300, cooldown_seconds=0)
        if move is None:
            break
        hunts = apply_move(hunts, move, now + cycle * 300)
        objectives.append(move.objective_after)
        moves += 1
    assert moves >= 9
    # A move across a plateau leaves the fine J equal, never higher.
    assert all(b <= a for a, b in itertools.pairwise(objectives))
    assert objectives[-1] < objectives[0] / 4
    end = flatness(pressure(load_curves(hunts, bucket=BUCKET, horizon=HOUR), 0.5))
    # Ten hunts over the 16 buckets the 480 s bound allows cannot beat 20 / 10 = 2.0.
    assert end <= 2.5
    assert all(0 <= h.offset < h.bound for h in hunts)
    # It stops on its own: the next cycle proposes nothing, so a settled fleet never thrashes.
    assert _propose(hunts, now=now + 100 * 300, cooldown_seconds=0) is None


@pytest.mark.parametrize(
    ("p", "width", "want"),
    [
        pytest.param([3.0, 1.0, 0.0, 0.0], 3, [4 / 3, 4 / 3, 1 / 3, 1.0], id="wraps_the_ends"),
        pytest.param([3.0, 1.0, 0.0, 0.0], 1, [3.0, 1.0, 0.0, 0.0], id="width_one_is_identity"),
        pytest.param([1.0, 1.0, 1.0], 3, [1.0, 1.0, 1.0], id="flat_stays_flat"),
    ],
)
def test_smoothed_is_a_circular_blur_that_keeps_the_total(p, width, want):
    out = smoothed(np.array(p), width)
    assert out.tolist() == pytest.approx(want)
    assert out.sum() == pytest.approx(sum(p))


@pytest.mark.parametrize(
    ("before", "after", "want"),
    [
        pytest.param((10.0, 5.0), (9.0, 5.0), True, id="fine_scale_drops"),
        pytest.param((10.0, 5.0), (10.0, 4.0), True, id="fine_equal_coarse_drops"),
        pytest.param((10.0, 5.0), (10.0, 5.0), False, id="all_equal"),
        pytest.param((10.0, 5.0), (11.0, 1.0), False, id="fine_rises_whatever_coarse_does"),
        pytest.param((10.0, 5.0), (9.95, 5.0), False, id="fine_drop_inside_the_margin"),
    ],
)
def test_improves_reads_the_finest_scale_that_differs(before, after, want):
    assert improves(before, after, 0.01) is want


def test_one_hunt_moves_per_cycle_so_identical_hunts_never_herd():
    hunts = [_hunt(f"h{i}", 600, 0) for i in range(100)]
    first = _propose(hunts, cooldown_seconds=0)
    assert first is not None
    hunts = apply_move(hunts, first, 0)
    assert sum(h.offset != 0 for h in hunts) == 1
    for cycle in range(1, 40):
        move = _propose(hunts, now=cycle, cooldown_seconds=0)
        assert move is not None
        hunts = apply_move(hunts, move, cycle)
    # Every move takes one hunt off the tallest bucket, so 40 moves leave 60 there.
    assert sum(h.offset == 0 for h in hunts) == 60


@pytest.mark.parametrize(
    ("hysteresis", "expect_move"),
    [
        pytest.param(0.0, True, id="any_gain_accepted"),
        pytest.param(0.50, True, id="gain_above_the_margin"),
        pytest.param(0.55, False, id="gain_below_the_margin_is_held"),
    ],
)
def test_hysteresis_holds_a_move_whose_gain_is_too_small(hysteresis, expect_move):
    # Two equal hunts stacked in 6 of 120 buckets: P is 20 there (scaled by its mean
    # of 0.1), so J is 2400 over a floor of 120. Parting them puts P at 10 in 12
    # buckets, J 1200, so the variance part falls from 2280 to 1080, a 52.6% gain.
    hunts = [_hunt("a", 600, 0), _hunt("b", 600, 0)]
    p = pressure(load_curves(hunts, bucket=BUCKET, horizon=HOUR), 0.5)
    assert objective(p) - objective_floor(p) == pytest.approx(2400 - 120)
    assert (_propose(hunts, hysteresis=hysteresis) is not None) is expect_move


@pytest.mark.parametrize(
    ("moved_at", "now", "expect_move"),
    [
        pytest.param(None, 1000, True, id="never_moved"),
        pytest.param(900, 1000, False, id="moved_inside_cooldown"),
        pytest.param(300, 1000, True, id="cooldown_elapsed"),
    ],
)
def test_cooldown_holds_a_hunt_just_moved(moved_at, now, expect_move):
    hunts = [_hunt("a", 600, 0, moved_at=moved_at), _hunt("b", 600, 0, moved_at=moved_at)]
    assert (_propose(hunts, now=now, cooldown_seconds=600) is not None) is expect_move


@pytest.mark.parametrize(
    ("memory_cap", "expect_move"),
    [
        pytest.param(None, True, id="no_cap"),
        pytest.param(3e9, True, id="cap_above_every_candidate"),
        pytest.param(2.2e9, False, id="cap_blocks_every_candidate"),
    ],
)
def test_the_memory_cap_holds_a_move_that_would_stack_memory(memory_cap, expect_move):
    # Judged on CPU alone, parting a and c pays. Every offset either can reach lands its
    # 90 s pulse on a 1.5 GB hunt, lifting that bucket to 2.5 GB over today's 2 GB peak.
    hunts = [_hunt("a", 600, 0, duration_ms=90_000), _hunt("c", 600, 0, duration_ms=90_000)]
    hunts += [_hunt("s1", 600, 90, mem=1.5e9, duration_ms=1000)]
    hunts += [_hunt("s2", 600, 120, mem=1.5e9, duration_ms=1000)]
    move = _propose(hunts, weight_memory=0.0, memory_cap=memory_cap, hysteresis=0.0)
    assert (move is not None) is expect_move


@pytest.mark.parametrize(
    "hunts",
    [
        pytest.param([], id="empty_fleet"),
        pytest.param([_hunt("z", mem=0, duration_ms=0)], id="zero_cost_fleet"),
        pytest.param([_hunt("one", 600, 0)], id="a_lone_hunt_has_nothing_to_dodge"),
    ],
)
def test_a_fleet_with_nothing_to_flatten_moves_nothing(hunts):
    assert _propose(hunts) is None


def test_cpu_is_used_only_when_every_hunt_carries_it():
    with_cpu = [_hunt("a", cpu_us=5e6), _hunt("b", cpu_us=1e6)]
    mixed = [_hunt("a", cpu_us=5e6), _hunt("b")]
    assert load_curves(with_cpu, bucket=BUCKET, horizon=HOUR).cpu.sum() == pytest.approx(
        (5e6 + 1e6) * 6
    )
    assert load_curves(mixed, bucket=BUCKET, horizon=HOUR).cpu.sum() == pytest.approx(
        20_000 * 2 * 6
    )


def test_flatness_is_one_for_a_flat_curve_and_zero_for_an_empty_one():
    assert flatness(np.ones(10)) == pytest.approx(1.0)
    assert flatness(np.zeros(10)) == 0.0
    assert flatness(np.array([])) == 0.0
