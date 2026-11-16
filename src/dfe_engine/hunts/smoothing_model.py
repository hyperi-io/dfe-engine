#  Project:      dfe-engine
#  File:         hunts/smoothing_model.py
#  Purpose:      The load curve, objective and one-hunt move the phase smoother decides on
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The maths of hunt schedule smoothing, with no ClickHouse in it.

Every rate hunt fires ``H / interval`` times across the hyperperiod ``H``, each fire
depositing a cost pulse ``width`` buckets wide onto the memory and CPU curves. The
composite pressure ``P`` weights the two curves, each scaled by its own mean so a
byte and a microsecond compare, and the objective ``J`` is the sum of ``P`` squared.
The total of each curve never changes when an offset moves, so a lower ``J`` is a
flatter curve and nothing else (docs/data-plane/hunt-schedule-smoothing.md, the
maths section).

:func:`propose_move` is one step of coordinate descent: take the tallest bucket,
and move ONE hunt firing into it by a bounded step to the offset that lowers ``J``
most. A move is kept only past a hysteresis margin, a hunt just moved sits out a
cool-down, and no move may push a bucket's memory over the cap. One move per cycle
is what keeps a fleet from herding into the same trough.

The controller in :mod:`dfe_engine.hunts.smoothing` reads the costs, calls this,
and writes the result. Everything here is deterministic on its inputs. The maths is
Derek's, worked by hand in his notebook and set out in the design doc.
"""

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True, slots=True)
class Cost:
    """What one fire of a hunt costs, the trailing p95 over the window.

    Attributes:
        mem_bytes: Peak memory of one fire.
        duration_ms: Duration of one fire, which stands in for CPU when
            ``cpu_us`` is None.
        cpu_us: CPU time of one fire, or None when the archive has no column for it.
        fires: Fires the p95 was taken over.
    """

    mem_bytes: float
    duration_ms: float
    cpu_us: float | None
    fires: int


@dataclass(frozen=True, slots=True)
class RateHunt:
    """A rate hunt as the optimiser sees it.

    Attributes:
        hunt_id: The hunt.
        interval: Its interval in seconds.
        offset: Seconds into the interval it fires.
        bound: Offsets live in ``[0, bound)``, the same window the hash offset uses.
        cost: Its measured cost.
        moved_at: Epoch seconds its offset last changed, or None if never moved.
    """

    hunt_id: str
    interval: int
    offset: int
    bound: int
    cost: Cost
    moved_at: int | None = None


@dataclass(frozen=True, slots=True)
class Curves:
    """The memory and CPU load curves over the hyperperiod, one value per bucket.

    Attributes:
        mem: Summed peak memory in each bucket.
        cpu: Summed CPU in each bucket, duration when the costs carry no CPU.
        bucket: Bucket width in seconds.
        horizon: The hyperperiod the curves span, in seconds.
    """

    mem: np.ndarray
    cpu: np.ndarray
    bucket: int
    horizon: int


@dataclass(frozen=True, slots=True)
class Move:
    """One accepted offset change.

    Attributes:
        hunt_id: The hunt moved.
        old_offset: Where it fired.
        new_offset: Where it fires now.
        objective_before: ``J`` before the move.
        objective_after: ``J`` after it.
    """

    hunt_id: str
    old_offset: int
    new_offset: int
    objective_before: float
    objective_after: float


def hyperperiod(intervals: Iterable[int], cap: int) -> tuple[int, bool]:
    """The least common multiple of the intervals, held to *cap*.

    Returns:
        The hyperperiod and whether the cap cut it. A cut hyperperiod wraps a
        hunt whose interval does not divide it, so its pulses land approximately.
    """
    period = 1
    for interval in intervals:
        period = math.lcm(period, max(1, interval))
        if period > cap:
            return cap, True
    return period, False


def pulse_width(duration_ms: float, bucket: int) -> int:
    """Buckets one fire covers, at least one."""
    return max(1, math.ceil(duration_ms / 1000 / bucket))


def bucket_count(horizon: int, bucket: int) -> int:
    """Buckets in the hyperperiod, the last one partial when the bucket does not divide it."""
    return max(1, math.ceil(horizon / bucket))


def covered_buckets(hunt: RateHunt, offset: int, *, bucket: int, horizon: int) -> np.ndarray:
    """The bucket index of every (fire, pulse step) of *hunt* firing at *offset*.

    A bucket covered twice appears twice, so a hunt that runs longer than its
    interval loads itself. Indices wrap around the hyperperiod.
    """
    buckets = bucket_count(horizon, bucket)
    fires = np.arange(math.ceil(horizon / hunt.interval)) * hunt.interval + offset
    starts = (fires % horizon) // bucket
    steps = np.arange(pulse_width(hunt.cost.duration_ms, bucket))
    return ((starts[:, None] + steps[None, :]) % buckets).ravel()


def _cpu_magnitude(cost: Cost, use_cpu: bool) -> float:
    return cost.cpu_us if use_cpu and cost.cpu_us is not None else cost.duration_ms


def _use_cpu(hunts: Sequence[RateHunt]) -> bool:
    """CPU is comparable across the fleet only when every hunt carries it."""
    return bool(hunts) and all(h.cost.cpu_us is not None for h in hunts)


def load_curves(hunts: Sequence[RateHunt], *, bucket: int, horizon: int) -> Curves:
    """Sum every hunt's pulses into the memory and CPU curves."""
    buckets = bucket_count(horizon, bucket)
    mem = np.zeros(buckets)
    cpu = np.zeros(buckets)
    use_cpu = _use_cpu(hunts)
    for hunt in hunts:
        covered = covered_buckets(hunt, hunt.offset, bucket=bucket, horizon=horizon)
        np.add.at(mem, covered, hunt.cost.mem_bytes)
        np.add.at(cpu, covered, _cpu_magnitude(hunt.cost, use_cpu))
    return Curves(mem=mem, cpu=cpu, bucket=bucket, horizon=horizon)


def _scaled(curve: np.ndarray) -> np.ndarray:
    """The curve over its own mean, so memory and CPU share a scale. A flat zero stays zero."""
    mean = float(curve.mean())
    return curve / mean if mean > 0 else curve


def pressure(curves: Curves, weight_memory: float) -> np.ndarray:
    """The composite pressure ``P``: the memory and CPU curves scaled and weighted."""
    return weight_memory * _scaled(curves.mem) + (1 - weight_memory) * _scaled(curves.cpu)


def objective(p: np.ndarray) -> float:
    """``J``, the sum of squared pressure, which is the curve's variance up to a constant."""
    return float(np.dot(p, p))


def flatness(p: np.ndarray) -> float:
    """Peak over mean of the pressure, 1.0 when flat and 0.0 for an empty curve."""
    mean = float(p.mean()) if p.size else 0.0
    return float(p.max()) / mean if mean > 0 else 0.0


def objective_floor(p: np.ndarray) -> float:
    """``T^2 / B``, the part of ``J`` no move can lower, since the total ``T`` is invariant.

    ``J`` less this floor is ``B`` times the variance of the curve, the only part a
    move changes, so the hysteresis margin is taken against it. A margin on ``J``
    itself goes blind once the curve is half flat, because the floor then dominates.
    """
    return float(p.sum()) ** 2 / p.size if p.size else 0.0


# The curve is also scored smoothed this many buckets wide, finest first, to break ties.
SCALES = (1, 3, 7, 15)


def smoothed(p: np.ndarray, width: int) -> np.ndarray:
    """The curve under a circular box blur *width* buckets wide, its total unchanged."""
    if width <= 1 or p.size == 0:
        return p
    half = width // 2
    wrapped = np.concatenate([p[-half:], p, p[:half]])
    return np.convolve(wrapped, np.full(width, 1 / width), mode="valid")[: p.size]


def variances(p: np.ndarray) -> tuple[float, ...]:
    """``J`` less its floor at every width in :data:`SCALES`, finest first.

    Two offsets that stack the same hunts leave the fine ``J`` equal, and a fleet of
    alike hunts then sits on a plateau no single bounded move crosses. The smoothed
    scores tell those moves apart: a blurred curve slopes toward a hole several
    buckets away, so the move that walks a hunt toward it wins the tie, and the hunt
    crosses the plateau one bounded step per cycle.
    """
    scores = []
    for width in SCALES:
        blurred = smoothed(p, width)
        scores.append(objective(blurred) - objective_floor(blurred))
    return tuple(scores)


def improves(before: tuple[float, ...], after: tuple[float, ...], hysteresis: float) -> bool:
    """Whether *after* beats *before* at the finest scale where they differ past the margin.

    A scale where the two agree within a relative tolerance defers to the next coarser
    one, so an equal fine ``J`` with a flatter coarse curve is an improvement, and a
    worse fine ``J`` never is.
    """
    for a, b in zip(before, after, strict=True):
        if math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-12):
            continue
        return b < a * (1 - hysteresis)
    return False


def _candidate_offsets(hunt: RateHunt, *, bucket: int, max_step: int) -> list[int]:
    """Offsets within one step of the current one, a bucket apart, inside the bound."""
    steps = max_step // bucket
    candidates = []
    for k in range(-steps, steps + 1):
        if k == 0:
            continue
        offset = hunt.offset + k * bucket
        if 0 <= offset < hunt.bound:
            candidates.append(offset)
    return candidates


def _in_cooldown(hunt: RateHunt, now: int, cooldown_seconds: float) -> bool:
    return hunt.moved_at is not None and now - hunt.moved_at < cooldown_seconds


def propose_move(
    hunts: Sequence[RateHunt],
    *,
    bucket: int,
    horizon: int,
    weight_memory: float,
    max_step_fraction: float,
    hysteresis: float,
    memory_cap: float | None,
    now: int,
    cooldown_seconds: float,
) -> Move | None:
    """One step of coordinate descent on ``J``, or None when no move is worth making.

    The hunts firing into the tallest bucket are tried heaviest first, then those in
    the next tallest, down to the mean, skipping any in cool-down. A hunt in a trough
    never moves. For each, every offset within ``max_step_fraction`` of its interval
    and a bucket apart is scored by recomputing only that hunt's pulses.
    The first hunt with a move that :func:`improves` the variance scores by more
    than ``hysteresis``, and keeps every bucket's memory under ``memory_cap`` or no
    higher than today's peak, is moved. Among its offsets the best scores win, and
    ties go to the shorter step.

    Args:
        hunts: The fleet, with its current offsets and costs.
        bucket: Bucket width in seconds.
        horizon: The hyperperiod in seconds.
        weight_memory: Weight of the memory curve in ``P``.
        max_step_fraction: The step bound as a share of each hunt's interval.
        hysteresis: The least relative drop in ``J`` that keeps a move.
        memory_cap: The memory no bucket may exceed, or None for no constraint.
        now: Epoch seconds, for the cool-down.
        cooldown_seconds: How long a moved hunt sits out.

    Returns:
        The move to commit, or None.
    """
    if not hunts:
        return None
    curves = load_curves(hunts, bucket=bucket, horizon=horizon)
    mem_mean = float(curves.mem.mean())
    cpu_mean = float(curves.cpu.mean())
    if mem_mean <= 0 and cpu_mean <= 0:
        return None
    use_cpu = _use_cpu(hunts)

    def magnitude(hunt: RateHunt) -> float:
        mem_part = hunt.cost.mem_bytes / mem_mean if mem_mean > 0 else 0.0
        cpu_part = _cpu_magnitude(hunt.cost, use_cpu) / cpu_mean if cpu_mean > 0 else 0.0
        return weight_memory * mem_part + (1 - weight_memory) * cpu_part

    p = pressure(curves, weight_memory)
    before = variances(p)
    mem_peak = float(curves.mem.max())
    cover = {h.hunt_id: covered_buckets(h, h.offset, bucket=bucket, horizon=horizon) for h in hunts}
    mean_pressure = float(p.mean())
    candidates: list[RateHunt] = []
    seen: set[str] = set()
    for tall in np.argsort(-p, kind="stable"):
        if p[tall] <= mean_pressure:
            break
        here = [h for h in hunts if h.hunt_id not in seen and tall in cover[h.hunt_id]]
        here.sort(key=lambda h: (-magnitude(h), h.hunt_id))
        candidates.extend(here)
        seen.update(h.hunt_id for h in here)
    for hunt in candidates:
        if _in_cooldown(hunt, now, cooldown_seconds):
            continue
        weight = magnitude(hunt)
        p_without = p.copy()
        np.add.at(p_without, cover[hunt.hunt_id], -weight)
        mem_without = curves.mem.copy()
        np.add.at(mem_without, cover[hunt.hunt_id], -hunt.cost.mem_bytes)
        best: tuple[tuple[float, ...], int, int] | None = None
        max_step = int(hunt.interval * max_step_fraction)
        for offset in _candidate_offsets(hunt, bucket=bucket, max_step=max_step):
            moved = covered_buckets(hunt, offset, bucket=bucket, horizon=horizon)
            mem_after = mem_without.copy()
            np.add.at(mem_after, moved, hunt.cost.mem_bytes)
            mem_after_peak = float(mem_after.max())
            if memory_cap is not None and mem_after_peak > memory_cap and mem_after_peak > mem_peak:
                continue
            p_after = p_without.copy()
            np.add.at(p_after, moved, weight)
            key = (variances(p_after), abs(offset - hunt.offset), offset)
            if best is None or key < best:
                best = key
        if best is None or not improves(before, best[0], hysteresis):
            continue
        floor = objective_floor(p)
        return Move(
            hunt_id=hunt.hunt_id,
            old_offset=hunt.offset,
            new_offset=best[2],
            objective_before=before[0] + floor,
            objective_after=best[0][0] + floor,
        )
    return None
