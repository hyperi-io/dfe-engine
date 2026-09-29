#  Project:      dfe-engine
#  File:         tests/integration/test_hunt_schedule_scaler.py
#  Purpose:      Live-CH proof the deterministic-due scaler query counts reality
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The KEDA scaler query against real ClickHouse (no mocks).

Proves ``due_query`` (run via ``due_count``) implements the parity predicate the
unit test pins: it counts exactly the hunts a worker would run - due, not already
completed at this fire, and unclaimed - and it EXCLUDES tombstoned (deleted) hunts,
those with a future watermark (fire already done), and those holding an active
lease. Determinism against the server's real now() is achieved with interval=2,
offset=0 hunts (fire = now//2*2 <= now, so always due) and forced not-due cases.
The schedule and coordination tables are the ones the schema phase applies, from
the ``dfe_db`` fixture.
"""

from __future__ import annotations

from dfe_engine.hunt_runner import ChCoordinator, HuntSpec, due_count, publish_schedule


def _spec(hunt_id: str, interval: int = 2) -> HuntSpec:
    # interval=2 -> phase offset 0, so the current fire (now//2*2) is always <= now
    return HuntSpec(hunt_id=hunt_id, interval_seconds=interval)


def test_due_count_counts_exactly_the_runnable_hunts(ch_client, dfe_db):
    coord = ChCoordinator(ch_client, database=dfe_db, settle_seconds=0.0, sleep=lambda _s: None)
    coord.ensure_schema()  # hunt_lease / hunt_watermark / hunt_state (the LEFT JOINs)

    server_now = int(ch_client.query("SELECT toInt64(now())").result_rows[0][0])

    specs = {h: _spec(h) for h in ("due1", "due2", "notdue_wm", "notdue_lease", "gone")}
    assert publish_schedule(ch_client, dfe_db, specs) == 5

    # notdue_wm: watermark far in the future -> this fire already "done" -> NOT due
    coord.set_watermark("notdue_wm", server_now + 10**9)
    # notdue_lease: an active lease (lease_until = now + 300) -> running -> NOT due
    assert coord.try_claim("notdue_lease", fire=server_now, now=server_now) is True
    # 'gone' is removed from config and re-published -> tombstoned (enabled=0)
    del specs["gone"]
    publish_schedule(ch_client, dfe_db, specs)

    # Only due1 + due2 are due-and-unclaimed. gone is tombstoned, notdue_wm/lease excluded.
    assert due_count(ch_client, dfe_db) == 2


def test_due_count_is_zero_when_all_caught_up(ch_client, dfe_db):
    """Scale-to-zero: when every hunt's fire is completed, the backlog is 0."""
    coord = ChCoordinator(ch_client, database=dfe_db, settle_seconds=0.0, sleep=lambda _s: None)
    coord.ensure_schema()
    server_now = int(ch_client.query("SELECT toInt64(now())").result_rows[0][0])

    specs = {h: _spec(h) for h in ("h1", "h2", "h3")}
    publish_schedule(ch_client, dfe_db, specs)
    # advance every watermark past any plausible current fire -> nothing owed
    for h in specs:
        coord.set_watermark(h, server_now + 10**9)

    assert due_count(ch_client, dfe_db) == 0
