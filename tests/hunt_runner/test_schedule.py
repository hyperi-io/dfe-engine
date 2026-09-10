#  Project:      dfe-engine
#  File:         tests/hunt_runner/test_schedule.py
#  Purpose:      Deterministic-due parity: the KEDA scaler decides exactly as the worker
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Pure-logic proof that the scale-from-zero decision is RELIABLE.

Scale-to-zero is only safe if KEDA wakes a worker precisely when a worker would
find work, and scales to zero precisely when a worker would idle. That is a parity
claim between two expressions of the same arithmetic: the SQL in ``due_query`` and
the worker's own gate in ``runner.tick`` (via ``spread.latest_fire``).

This test encodes both and asserts they agree across a matrix of (hunt_id, interval,
now, watermark, lease). No ClickHouse needed - it is the arithmetic that must match;
the CH integration test then proves the SQL string implements this same predicate.
"""

from __future__ import annotations

from dfe_engine.hunt_runner.schedule import due_query
from dfe_engine.hunt_runner.spread import latest_fire, phase_offset


def _sql_predicate(now: int, interval: int, offset: int, wm: int | None, lu: int | None) -> bool:
    """Exactly what due_query's WHERE encodes (coalesce(NULL, 0) for absent rows)."""
    current = (now // interval) * interval + offset
    fire = current if now >= current else current - interval
    return (wm if wm is not None else 0) < fire and (lu if lu is not None else 0) <= now


def _worker_would_run(
    hunt_id: str, interval: int, now: int, wm: int | None, lu: int | None
) -> bool:
    """What runner.tick decides for one hunt, via the real spread functions.

    Mirrors runner.tick: skip if watermark >= the latest fire, skip if an active
    lease (lease_until > now).
    """
    fire = latest_fire(hunt_id, interval, now)
    if wm is not None and wm >= fire:
        return False
    if lu is not None and lu > now:
        return False
    return True


def test_scaler_predicate_matches_worker_across_matrix():
    hunt_ids = ["brute-force", "win_logon_anomaly", "x", "a-very-long-hunt-identifier-42", "z9"]
    intervals = [2, 30, 60, 300, 3600]
    for hunt_id in hunt_ids:
        for interval in intervals:
            offset = phase_offset(hunt_id, interval)
            # sample now across several intervals AND right around this hunt's fire
            base = 1_900_000_000  # a realistic epoch well past 1970
            nows = [
                base,
                base + offset - 1,
                base + offset,
                base + offset + 1,
                base + interval,
                base + interval + offset,
                base + 3 * interval + offset - 1,
                base + 5 * interval + offset,
            ]
            for now in nows:
                fire = latest_fire(hunt_id, interval, now)
                watermarks = [None, 0, fire - 1, fire, fire + 1, fire + interval, now + 10**9]
                leases = [None, now - 1, now, now + 1, now + 10**6]
                for wm in watermarks:
                    for lu in leases:
                        assert _sql_predicate(now, interval, offset, wm, lu) == _worker_would_run(
                            hunt_id, interval, now, wm, lu
                        ), f"parity broke: {hunt_id=} {interval=} {now=} {wm=} {lu=}"


def test_due_query_is_stable_sql_for_a_database():
    sql = due_query("dfe")
    # keys the scaler depends on - kept explicit so a refactor that drops one fails here
    assert "`dfe`.hunt_schedule" in sql
    assert "`dfe`.hunt_watermark" in sql
    assert "`dfe`.hunt_lease" in sql
    assert "enabled = 1" in sql  # tombstones excluded before any intDiv (no div-by-zero)
    assert "count() AS due" in sql
    # the fire arithmetic must match spread.latest_fire: this interval's fire once
    # it has arrived, else the previous interval's
    current = "(intDiv(toInt64(now()), s.interval_seconds) * s.interval_seconds + s.phase_offset)"
    assert f"if(toInt64(now()) >= {current}, {current}, {current} - s.interval_seconds)" in sql
