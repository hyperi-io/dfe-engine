#  Project:      dfe-engine
#  File:         tests/integration/test_hunt_runner_pipeline.py
#  Purpose:      Phase A - loader -> worker chain against live ClickHouse (no mocks)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Phase A: v1's loader->worker pipeline end to end on real ClickHouse.

Proves the new v1 wiring on a live cluster: a hunt YAML loads into a HuntSpec
(spec_loader), its windowed INSERT runs (worker) against real scratch tables, the
watermark advances (the crash-safe resume point), and the query is attributed in
system.query_log via log_comment (the cost-model enabler). Deterministic: the window
is pinned by pre-setting the watermark, so there is NO wall-clock due-timing race.
Isolated scratch database, dropped after. No mocks - a real clickhouse-connect client
from the ch_client fixture.
"""

from __future__ import annotations

import uuid

import pytest

from dfe_engine.hunt_runner import (
    ChCoordinator,
    HuntRunner,
    HuntWorker,
    load_specs,
    publish_schedule,
    run_loop,
)


@pytest.fixture
def scratch_db(ch_client):
    """An isolated CH database for the scratch tables + coordination; dropped after."""
    db = f"dfe_runner_{uuid.uuid4().hex[:8]}"
    ch_client.command(f"CREATE DATABASE `{db}`")
    try:
        yield db
    finally:
        try:
            ch_client.command(f"DROP DATABASE IF EXISTS `{db}`")
        except Exception:
            pass


def test_loaded_hunt_runs_windowed_insert_and_is_attributed(ch_client, scratch_db, tmp_path):
    # Scratch source + target tables (the hunt reads src, writes matched rows to tgt).
    ch_client.command(
        f"CREATE TABLE `{scratch_db}`.src (timestamp_load Int64, msg String) "
        "ENGINE = MergeTree ORDER BY timestamp_load"
    )
    ch_client.command(
        f"CREATE TABLE `{scratch_db}`.tgt (timestamp_load Int64, msg String) "
        "ENGINE = MergeTree ORDER BY timestamp_load"
    )
    ch_client.insert(
        f"{scratch_db}.src",
        [[100, "a"], [150, "b"], [250, "c"]],
        column_names=["timestamp_load", "msg"],
    )

    # A realistic rate-hunt YAML using the scratch tables - loaded through spec_loader.
    hunt_yaml = (
        'schedule:\n  mode: rate\n  interval: "60s"\n'
        f'query: "INSERT INTO `{scratch_db}`.tgt SELECT timestamp_load, msg '
        f'FROM `{scratch_db}`.src WHERE {{window}}"\n'
        f'global_target_table_name: "{scratch_db}.tgt"\n'
    )
    (tmp_path / "scratch_hunt.yaml").write_text(hunt_yaml)
    specs = load_specs(tmp_path)
    assert set(specs) == {"scratch_hunt"}
    spec = specs["scratch_hunt"]
    assert spec.interval_seconds == 60

    coord = ChCoordinator(ch_client, database=scratch_db, settle_seconds=0.0, sleep=lambda _s: None)
    coord.ensure_schema()
    # Pin the window to [100, 200) by pre-setting the watermark -> deterministic.
    coord.set_watermark("scratch_hunt", 100)

    worker = HuntWorker(ch_client, coord)
    new_wm = worker.run(spec, scheduled_start=200)

    # Watermark advances to the window end only AFTER the query commits (crash-safe).
    assert new_wm == 200
    assert coord.get_watermark("scratch_hunt") == 200

    # The windowed INSERT selected exactly the rows in [100, 200): 100 and 150, not 250.
    rows = ch_client.query(
        f"SELECT timestamp_load FROM `{scratch_db}`.tgt ORDER BY timestamp_load"
    ).result_rows
    assert [int(r[0]) for r in rows] == [100, 150]

    # The query is attributed in query_log via log_comment (the cost-model enabler).
    ch_client.command("SYSTEM FLUSH LOGS")
    tagged = ch_client.query(
        "SELECT count() FROM system.query_log "
        "WHERE log_comment = {c:String} AND type = 'QueryFinish'",
        parameters={"c": "hunt:scratch_hunt"},
    ).result_rows
    assert int(tagged[0][0]) >= 1


def test_empty_query_hunt_is_a_noop_that_still_advances_watermark(ch_client, scratch_db):
    # A hunt with no query (rule->SQL compilation is out of v1 scope) must not crash;
    # it advances the watermark so the schedule still progresses.
    coord = ChCoordinator(ch_client, database=scratch_db, settle_seconds=0.0, sleep=lambda _s: None)
    coord.ensure_schema()
    from dfe_engine.hunt_runner import HuntSpec

    spec = HuntSpec(hunt_id="noop", interval_seconds=60, query="")
    worker = HuntWorker(ch_client, coord)
    assert worker.run(spec, scheduled_start=500) == 500
    assert coord.get_watermark("noop") == 500


def test_daemon_loop_fires_a_loaded_hunt_once_against_live_ch(ch_client, scratch_db, tmp_path):
    """The WHOLE v1 runner end to end on live CH: materialise -> run_loop -> tick ->
    due -> claim -> execute -> watermark, and never-double-run on the next tick.

    Deterministic via a FIXED injected clock: interval 2s + hash offset 0 means the
    hunt's fire == the fixed 'now', so it is due on tick 1; tick 2 sees the watermark
    at that fire and does NOT re-run. One shared connection, so no multi-node split.
    """
    ch_client.command(
        f"CREATE TABLE `{scratch_db}`.src (timestamp_load Int64, msg String) "
        "ENGINE = MergeTree ORDER BY timestamp_load"
    )
    ch_client.command(
        f"CREATE TABLE `{scratch_db}`.tgt (timestamp_load Int64, msg String) "
        "ENGINE = MergeTree ORDER BY timestamp_load"
    )
    fixed_now = 1_000_000_000  # even -> fire == fixed_now for interval 2, offset 0
    ch_client.insert(
        f"{scratch_db}.src",
        [[fixed_now - 1, "in-window"], [fixed_now - 5, "too-old"]],
        column_names=["timestamp_load", "msg"],
    )

    hunt_yaml = (
        'schedule:\n  mode: rate\n  interval: "2s"\n'
        f'query: "INSERT INTO `{scratch_db}`.tgt SELECT timestamp_load, msg '
        f'FROM `{scratch_db}`.src WHERE {{window}}"\n'
    )
    (tmp_path / "live_hunt.yaml").write_text(hunt_yaml)
    specs = load_specs(tmp_path)
    assert specs["live_hunt"].interval_seconds == 2

    coord = ChCoordinator(ch_client, database=scratch_db, settle_seconds=0.0, sleep=lambda _s: None)
    coord.ensure_schema()
    # materialise the KEDA schedule + confirm the hunt is due before we run.
    publish_schedule(ch_client, scratch_db, specs)

    runner = HuntRunner(coord, HuntWorker(ch_client, coord), specs, cap=8)
    ticks = {"n": 0}

    def _tick(now: int) -> int:
        ticks["n"] += 1
        return runner.tick(now)

    run_loop(
        tick=_tick,
        should_stop=lambda: ticks["n"] >= 2,
        clock=lambda: float(fixed_now),
        sleep=lambda _s: None,
        poll_seconds=0.0,
    )

    # Fired exactly once: only the in-window row [fixed_now-2, fixed_now) landed.
    rows = ch_client.query(f"SELECT msg FROM `{scratch_db}`.tgt").result_rows
    assert [r[0] for r in rows] == ["in-window"]
    # The watermark advanced to the fire, so tick 2 correctly did NOT re-run.
    assert coord.get_watermark("live_hunt") == fixed_now
