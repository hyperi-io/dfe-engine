#  Project:      dfe-engine
#  File:         tests/integration/test_hunt_runner_e2e.py
#  Purpose:      End-to-end hunt-runner tick over REAL PG (claims) + CH (execute)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Full pull-loop: PG claim table + CH worker, against real databases (no mocks).

Proves the runtime end to end: a tick enqueues a due hunt, claims it (SKIP LOCKED),
the worker runs its windowed INSERT against ClickHouse, and the watermark advances
so the next tick resumes incrementally (no duplicate rows). Cleans up after itself.
"""

from __future__ import annotations

import uuid

import pytest

from dfe_engine.hunt_runner import (
    CheckpointStore,
    ClaimTable,
    HuntRunner,
    HuntSpec,
    HuntWorker,
)


@pytest.fixture
def pg_table(pg_conn):
    with pg_conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS hunt_run")
        cur.execute("DROP TABLE IF EXISTS hunt_state")
    pg_conn.commit()
    t = ClaimTable(pg_conn)
    t.init_schema()
    try:
        yield t
    finally:
        with pg_conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS hunt_run")
            cur.execute("DROP TABLE IF EXISTS hunt_state")
        pg_conn.commit()


@pytest.fixture
def ch_db(ch_client):
    """An isolated CH database with src/tgt tables; dropped after."""
    db = f"dfe_e2e_{uuid.uuid4().hex[:8]}"
    ch_client.command(f"CREATE DATABASE {db}")
    ch_client.command(
        f"CREATE TABLE {db}.src (timestamp_load Int64, ev String) ENGINE = MergeTree ORDER BY timestamp_load"
    )
    ch_client.command(f"CREATE TABLE {db}.tgt (ev String) ENGINE = MergeTree ORDER BY ev")
    try:
        yield db
    finally:
        try:
            ch_client.command(f"DROP DATABASE IF EXISTS {db}")
            ch_client.command("DROP DATABASE IF EXISTS dfe_audit")  # watermark table
        except Exception:
            pass


def _count(ch, table):
    return int(ch.query(f"SELECT count() FROM {table}").result_rows[0][0])


def test_full_tick_executes_and_resumes_incrementally(pg_table, pg_dsn, ch_client, ch_db):
    # source rows across two windows
    ch_client.command(
        f"INSERT INTO {ch_db}.src (timestamp_load, ev) VALUES (500,'a'),(900,'b'),(1200,'c')"
    )

    checkpoints = CheckpointStore(ch_client)
    checkpoints.ensure()
    worker = HuntWorker(ch_client, checkpoints)
    spec = HuntSpec(
        hunt_id="h1",
        interval_seconds=600,
        query=f"INSERT INTO {ch_db}.tgt SELECT ev FROM {ch_db}.src WHERE {{window}}",
    )
    runner = HuntRunner(pg_table, worker, {spec.hunt_id: spec}, cap=4)

    # enqueue + drain with explicit due_at (= window end) for a deterministic window
    pg_table.enqueue("h1", due_at=1000)
    assert runner.drain(now=1000) == 1  # window [400,1000) -> rows a,b (not c@1200)
    assert _count(ch_client, f"{ch_db}.tgt") == 2

    pg_table.enqueue("h1", due_at=1300)
    assert runner.drain(now=1300) == 1  # window [1000,1300) -> only c; a,b NOT re-run
    assert _count(ch_client, f"{ch_db}.tgt") == 3  # 2 + 1, no duplicates

    # watermark advanced to the last window end
    assert checkpoints.get("h1") == 1300


def test_tick_dedups_active_hunts(pg_table, ch_client, ch_db):
    checkpoints = CheckpointStore(ch_client)
    checkpoints.ensure()
    worker = HuntWorker(ch_client, checkpoints)
    spec = HuntSpec(hunt_id="h2", interval_seconds=600, query="")  # empty query = no-op
    runner = HuntRunner(pg_table, worker, {spec.hunt_id: spec}, cap=4)

    # pre-enqueue a pending run; a tick must NOT enqueue a duplicate
    pg_table.enqueue("h2", due_at=1)
    before = pg_table.active_hunt_ids()
    assert before == {"h2"}
    runner.tick(now=1000)  # executes the one pending run, no duplicate enqueued
    # after executing, the run is done -> not active
    assert "h2" not in pg_table.active_hunt_ids()
