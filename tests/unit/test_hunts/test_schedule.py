#  Project:      dfe-engine
#  File:         tests/unit/test_hunts/test_schedule.py
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

import pytest

from dfe_engine.hunts.hunt_spec import HuntSpec
from dfe_engine.hunts.schedule import (
    current_fire,
    due_query,
    latest_fire,
    learned_offsets,
    next_due,
    phase_offset,
    restore_hash_offsets,
)


def _sql_predicate(now: int, interval: int, offset: int, wm: int | None, lu: int | None) -> bool:
    """Exactly what due_query's WHERE encodes (coalesce(NULL, 0) for absent rows)."""
    current = (now // interval) * interval + offset
    fire = current if now >= current else current - interval
    return (wm if wm is not None else 0) < fire and (lu if lu is not None else 0) <= now


def _worker_would_run(
    hunt_id: str, interval: int, now: int, wm: int | None, lu: int | None, offset: int | None
) -> bool:
    """What runner.tick decides for one hunt, via the real spread functions.

    Mirrors runner.tick: skip if watermark >= the latest fire, skip if an active
    lease (lease_until > now).
    """
    fire = latest_fire(hunt_id, interval, now, offset=offset)
    if wm is not None and wm >= fire:
        return False
    if lu is not None and lu > now:
        return False
    return True


@pytest.mark.parametrize("learned", [False, True], ids=["hash_offset", "learned_offset"])
def test_scaler_predicate_matches_worker_across_matrix(learned):
    """KEDA reads hunt_schedule.phase_offset, so a learned offset keeps the parity too."""
    hunt_ids = ["brute-force", "win_logon_anomaly", "x", "a-very-long-hunt-identifier-42", "z9"]
    intervals = [2, 30, 60, 300, 3600]
    for hunt_id in hunt_ids:
        for interval in intervals:
            # A learned offset anywhere in the interval, including past the hash window.
            offset = (interval - 1) if learned else phase_offset(hunt_id, interval)
            explicit = offset if learned else None
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
                fire = latest_fire(hunt_id, interval, now, offset=explicit)
                watermarks = [None, 0, fire - 1, fire, fire + 1, fire + interval, now + 10**9]
                leases = [None, now - 1, now, now + 1, now + 10**6]
                for wm in watermarks:
                    for lu in leases:
                        assert _sql_predicate(now, interval, offset, wm, lu) == _worker_would_run(
                            hunt_id, interval, now, wm, lu, explicit
                        ), f"parity broke: {hunt_id=} {interval=} {now=} {wm=} {lu=}"


@pytest.mark.parametrize(
    ("offset", "want_fire"),
    [
        pytest.param(None, 600_000 + phase_offset("h", 600), id="none_means_the_hash"),
        pytest.param(0, 600_000, id="zero_is_an_offset_not_absence"),
        pytest.param(599, 600_599, id="anywhere_in_the_interval"),
    ],
)
def test_an_explicit_offset_wins_over_the_hash_in_every_fire_function(offset, want_fire):
    now = 600_000 + 599
    assert current_fire("h", 600, now, offset=offset) == want_fire
    assert latest_fire("h", 600, now, offset=offset) == want_fire
    assert next_due("h", 600, want_fire, offset=offset) == want_fire + 600


def test_learned_offsets_keep_only_hunts_whose_interval_is_unchanged():
    specs = {
        "same": HuntSpec(hunt_id="same", interval_seconds=600),
        "changed": HuntSpec(hunt_id="changed", interval_seconds=900),
    }
    schedule = {"same": (600, 123), "changed": (600, 45), "gone": (600, 7)}
    assert learned_offsets(schedule, specs) == {"same": 123}


class _ScheduleClient:
    """A client answering read_schedule from a fixed table and recording inserts."""

    def __init__(self, rows: list[tuple[str, int, int]]) -> None:
        self._rows = rows
        self.inserted: list[list] = []

    def query(self, sql: str, **_kwargs):
        # The insert settings probe reads system.settings, which holds no schedule rows.
        rows = [(0,)] if "system.settings" in sql else [(h, i, o, 1) for h, i, o in self._rows]
        return type("R", (), {"result_rows": rows})()

    def insert(self, _table: str, rows: list[list], **_kwargs) -> None:
        self.inserted.extend(rows)

    def command(self, *_args, **_kwargs) -> None:
        pass


@pytest.mark.parametrize(
    ("rows", "want_rewritten"),
    [
        pytest.param([], [], id="empty_table_stays_empty"),
        pytest.param([("a", 600, phase_offset("a", 600))], [], id="hash_rows_untouched"),
        pytest.param(
            [("a", 600, phase_offset("a", 600) + 1), ("b", 600, phase_offset("b", 600))],
            [["a", 600, phase_offset("a", 600), 1]],
            id="only_the_learned_row_is_rewritten",
        ),
        pytest.param([("t", 0, 5)], [], id="a_tombstone_interval_is_skipped"),
    ],
)
def test_restoring_the_hash_rewrites_only_rows_that_differ(rows, want_rewritten):
    client = _ScheduleClient(rows)
    assert restore_hash_offsets(client, "dfe") == len(want_rewritten)
    assert client.inserted == want_rewritten


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
