#  Project:      dfe-engine
#  File:         tests/integration/test_hunt_run_visibility.py
#  Purpose:      Run-now and the last run's row count, on real ClickHouse
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""What the runner records about a run, and what the API can read back.

Two claims, both against a live ClickHouse because the whole point is that the
coordination tables carry this and nothing else does:

- the INSERT's row count survives the run and comes back per hunt, and a run that
  matched nothing is reported as 0 rather than as a hunt that never ran
- a run queued through the coordination state is claimed by the NEXT tick, with no
  push and no listener, even though the hunt's own schedule says it is done for
  this interval

The read model here is the same call the hunts list makes, so what the page would
show is what is asserted.
"""

from __future__ import annotations

import uuid

import pytest

from dfe_engine.hunt_runner import (
    ChCoordinator,
    HuntRunner,
    HuntSpec,
    HuntWorker,
    read_run_status,
)
from dfe_engine.hunt_runner.spread import current_fire


@pytest.fixture
def scratch_db(ch_client):
    """An isolated CH database with a source and a target table; dropped after."""
    db = f"dfe_runvis_{uuid.uuid4().hex[:8]}"
    ch_client.command(f"CREATE DATABASE `{db}`")
    ch_client.command(
        f"CREATE TABLE `{db}`.src (timestamp_load Int64, ev String) "
        "ENGINE = MergeTree ORDER BY timestamp_load"
    )
    ch_client.command(f"CREATE TABLE `{db}`.tgt (ev String) ENGINE = MergeTree ORDER BY ev")
    try:
        yield db
    finally:
        try:
            ch_client.command(f"DROP DATABASE IF EXISTS `{db}`")
        except Exception:
            pass


def _spec(hunt_id: str, db: str) -> HuntSpec:
    return HuntSpec(
        hunt_id=hunt_id,
        interval_seconds=600,
        queries=[f"INSERT INTO `{db}`.tgt SELECT ev FROM `{db}`.src WHERE {{window}}"],
        timestamp_field="timestamp_load",
    )


def _coord(ch_client, db: str) -> ChCoordinator:
    coord = ChCoordinator(ch_client, database=db, settle_seconds=0.0, sleep=lambda _s: None)
    coord.ensure_schema()
    return coord


def test_the_row_count_a_run_wrote_is_recorded_and_read_back(ch_client, scratch_db):
    hunt = f"rows_{uuid.uuid4().hex[:8]}"
    coord = _coord(ch_client, scratch_db)
    runner = HuntRunner(coord, HuntWorker(ch_client, coord), {hunt: _spec(hunt, scratch_db)}, cap=4)

    fire = current_fire(hunt, 600, 5_000_000)
    ch_client.command(
        f"INSERT INTO `{scratch_db}`.src (timestamp_load, ev) VALUES "
        f"({fire - 100},'a'),({fire - 50},'b'),({fire - 5000},'too-old')"
    )
    assert runner.tick(fire + 1) == 1

    status = read_run_status(ch_client, scratch_db, [hunt], now=fire + 1)[hunt]
    # Two rows in the window, and the third outside it, so this is the INSERT's own
    # count rather than the table's.
    assert status.last_run_rows == 2
    assert status.last_run == fire
    assert status.running is False
    assert status.run_requested is False


def test_a_run_that_matched_nothing_reads_as_zero_not_as_never_run(ch_client, scratch_db):
    hunt = f"quiet_{uuid.uuid4().hex[:8]}"
    coord = _coord(ch_client, scratch_db)
    runner = HuntRunner(coord, HuntWorker(ch_client, coord), {hunt: _spec(hunt, scratch_db)}, cap=4)

    fire = current_fire(hunt, 600, 5_000_000)
    before = read_run_status(ch_client, scratch_db, [hunt], now=fire)[hunt]
    assert before.last_run is None
    assert before.last_run_rows is None

    assert runner.tick(fire + 1) == 1  # source is empty for this window
    after = read_run_status(ch_client, scratch_db, [hunt], now=fire + 1)[hunt]
    assert after.last_run == fire
    assert after.last_run_rows == 0


def test_a_queued_run_is_claimed_by_the_next_tick(ch_client, scratch_db):
    hunt = f"runnow_{uuid.uuid4().hex[:8]}"
    coord = _coord(ch_client, scratch_db)
    runner = HuntRunner(coord, HuntWorker(ch_client, coord), {hunt: _spec(hunt, scratch_db)}, cap=4)

    fire = current_fire(hunt, 600, 5_000_000)
    assert runner.tick(fire + 1) == 1
    # This interval's fire is done, so the schedule alone has nothing left to run.
    assert runner.tick(fire + 2) == 0

    requested = fire + 3
    coord.request_run(hunt, requested)
    assert coord.pending_runs(requested)[hunt] == requested
    assert read_run_status(ch_client, scratch_db, [hunt], now=requested)[hunt].run_requested is True

    ch_client.command(
        f"INSERT INTO `{scratch_db}`.src (timestamp_load, ev) VALUES ({fire + 1},'adhoc')"
    )
    # No push and no restart: the next ordinary tick finds the request and runs it.
    assert runner.tick(requested + 1) == 1
    assert coord.get_watermark(hunt) == requested

    rows = ch_client.query(f"SELECT ev FROM `{scratch_db}`.tgt").result_rows
    assert [r[0] for r in rows] == ["adhoc"]
    status = read_run_status(ch_client, scratch_db, [hunt], now=requested + 1)[hunt]
    assert status.last_run == requested
    assert status.last_run_rows == 1
    # The request is served, so it must not be claimed a second time.
    assert coord.pending_runs(requested + 1) == {}
    assert runner.tick(requested + 2) == 0


def test_the_too_aggressive_flag_reaches_the_read_model(ch_client, scratch_db):
    hunt = f"late_{uuid.uuid4().hex[:8]}"
    coord = _coord(ch_client, scratch_db)
    coord.record_overrun(hunt)
    status = read_run_status(ch_client, scratch_db, [hunt], now=5_000_000)[hunt]
    assert status.too_aggressive is True
    assert status.overrun_count == 1
