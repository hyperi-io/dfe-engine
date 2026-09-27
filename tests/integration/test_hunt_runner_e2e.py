#  Project:      dfe-engine
#  File:         tests/integration/test_hunt_runner_e2e.py
#  Purpose:      End-to-end hunt-runner over REAL ClickHouse only (no Postgres)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Full hunt loop over real ClickHouse alone (no Postgres, no mocks).

Proves the runtime on CH only: the worker runs a windowed INSERT and advances the
watermark so the next window resumes incrementally (no duplicate rows), and a
runner tick claims + runs a due hunt while NEVER double-running one that is already
leased (records the too-aggressive overrun instead). Uses the ClickHouse the .env
configures, else a docker ClickHouse; drops its isolated database after.
"""

from __future__ import annotations

import uuid

import pytest

from dfe_engine.hunt_runner import ChCoordinator, HuntRunner, HuntSpec, HuntWorker
from dfe_engine.hunt_runner.spread import current_fire


@pytest.fixture
def ch_db(ch_client):
    """An isolated CH database with src/tgt tables; dropped after."""
    db = f"dfe_e2e_{uuid.uuid4().hex[:8]}"
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


def _count(ch, table: str) -> int:
    return int(ch.query(f"SELECT count() FROM {table}").result_rows[0][0])


def _coord(ch, db, worker_id="w1"):
    return ChCoordinator(
        ch, database=db, worker_id=worker_id, settle_seconds=0.0, sleep=lambda _s: None
    )


def test_worker_executes_and_resumes_incrementally(ch_client, ch_db):
    ch_client.command(
        f"INSERT INTO `{ch_db}`.src (timestamp_load, ev) VALUES (500,'a'),(900,'b'),(1200,'c')"
    )
    coord = _coord(ch_client, ch_db)
    coord.ensure_schema()
    worker = HuntWorker(ch_client, coord)
    spec = HuntSpec(
        hunt_id="h1",
        interval_seconds=600,
        queries=[f"INSERT INTO `{ch_db}`.tgt SELECT ev FROM `{ch_db}`.src WHERE {{window}}"],
        timestamp_field="timestamp_load",
    )

    # first window [400,1000) -> a,b (not c@1200)
    assert worker.run(spec, scheduled_start=1000) == 1000
    assert _count(ch_client, f"`{ch_db}`.tgt") == 2
    # next window [1000,1300) -> only c; a,b are NOT re-run
    assert worker.run(spec, scheduled_start=1300) == 1300
    assert _count(ch_client, f"`{ch_db}`.tgt") == 3  # 2 + 1, no duplicates
    assert coord.get_watermark("h1") == 1300


def test_tick_runs_due_hunt_and_frees_slot(ch_client, ch_db):
    ch_client.command(f"INSERT INTO `{ch_db}`.src (timestamp_load, ev) VALUES (10,'x'),(20,'y')")
    coord = _coord(ch_client, ch_db)
    coord.ensure_schema()
    worker = HuntWorker(ch_client, coord)
    spec = HuntSpec(
        hunt_id="h2",
        interval_seconds=600,
        queries=[f"INSERT INTO `{ch_db}`.tgt SELECT ev FROM `{ch_db}`.src WHERE {{window}}"],
        timestamp_field="timestamp_load",
    )
    runner = HuntRunner(coord, worker, {spec.hunt_id: spec}, cap=4)

    fire = current_fire("h2", 600, 5000)
    now = fire + 1  # guaranteed due (now >= this interval's fire)
    assert runner.tick(now) == 1
    assert coord.active_count(now) == 0  # slot released after the run
    # a second tick in the SAME interval must not re-run (fire already completed)
    assert runner.tick(now) == 0


def test_tick_never_double_runs_a_leased_hunt(ch_client, ch_db):
    coord = _coord(ch_client, ch_db, "runner")
    coord.ensure_schema()
    worker = HuntWorker(ch_client, coord)
    spec = HuntSpec(hunt_id="h3", interval_seconds=600)  # never reached: the lease blocks it
    runner = HuntRunner(coord, worker, {spec.hunt_id: spec}, cap=4)

    fire = current_fire("h3", 600, 5000)
    now = fire + 1
    # a DIFFERENT worker holds an active lease (a prior fire still running)
    other = _coord(ch_client, ch_db, "other")
    assert other.try_claim("h3", fire=fire, now=now) is True
    # the runner must NOT double-run it - it records an overrun and skips
    assert runner.tick(now) == 0
    state = coord.get_state("h3")
    assert state is not None
    assert state.too_aggressive is True
