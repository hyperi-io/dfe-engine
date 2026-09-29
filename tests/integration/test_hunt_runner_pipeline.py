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
The scratch tables sit in the ``dfe_db`` database, which carries the coordination
tables the schema phase applies. No mocks - a real clickhouse-connect client from
the ch_client fixture.
"""

from __future__ import annotations

import pytest

from dfe_engine.hunt_runner import (
    ChCoordinator,
    HuntRunner,
    HuntWorker,
    load_specs,
    publish_schedule,
    run_loop,
)


def test_loaded_hunt_runs_windowed_insert_and_is_attributed(ch_client, dfe_db, tmp_path):
    # Scratch source + target tables (the hunt reads src, writes matched rows to tgt).
    ch_client.command(
        f"CREATE TABLE `{dfe_db}`.src (timestamp_load Int64, msg String) "
        "ENGINE = MergeTree ORDER BY timestamp_load"
    )
    ch_client.command(
        f"CREATE TABLE `{dfe_db}`.tgt (timestamp_load Int64, msg String) "
        "ENGINE = MergeTree ORDER BY timestamp_load"
    )
    ch_client.insert(
        f"{dfe_db}.src",
        [[100, "a"], [150, "b"], [250, "c"]],
        column_names=["timestamp_load", "msg"],
    )

    # A realistic rate-hunt YAML using the scratch tables - loaded through spec_loader.
    hunt_yaml = (
        'schedule:\n  mode: rate\n  interval: "60s"\n'
        f'query: "INSERT INTO `{dfe_db}`.tgt SELECT timestamp_load, msg '
        f'FROM `{dfe_db}`.src WHERE {{window}}"\n'
        f'global_target_table_name: "{dfe_db}.tgt"\n'
        'timestamp_field: "timestamp_load"\n'
    )
    (tmp_path / "scratch_hunt.yaml").write_text(hunt_yaml)
    specs = load_specs(tmp_path)
    assert set(specs) == {"scratch_hunt"}
    spec = specs["scratch_hunt"]
    assert spec.interval_seconds == 60

    coord = ChCoordinator(ch_client, database=dfe_db, settle_seconds=0.0, sleep=lambda _s: None)
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
        f"SELECT timestamp_load FROM `{dfe_db}`.tgt ORDER BY timestamp_load"
    ).result_rows
    assert [int(r[0]) for r in rows] == [100, 150]

    # The query is attributed in query_log via a JSON DfeQueryTags log_comment (the
    # cost-model enabler). The query_log_archive MV keeps only isValidJSON + tagged
    # rows, so asserting the JSON shape here is what proves the hunt lands in the
    # cost leaderboard (feature='hunts', id=the hunt).
    ch_client.command("SYSTEM FLUSH LOGS")
    tagged = ch_client.query(
        "SELECT count() FROM system.query_log "
        "WHERE isValidJSON(log_comment) "
        "AND JSONExtractString(log_comment, 'feature') = 'hunts' "
        "AND JSONExtractString(log_comment, 'id') = {c:String} "
        "AND type = 'QueryFinish'",
        parameters={"c": "scratch_hunt"},
    ).result_rows
    assert int(tagged[0][0]) >= 1


def test_a_hunt_with_no_query_fails_and_leaves_its_watermark_where_it_was(ch_client, dfe_db):
    # A hunt whose rules compiled to nothing has not run. Advancing the watermark
    # would move it past a window nothing scanned, and every fire would look clean.
    coord = ChCoordinator(ch_client, database=dfe_db, settle_seconds=0.0, sleep=lambda _s: None)
    coord.ensure_schema()
    from dfe_engine.hunt_runner import EmptyHuntQuery, HuntSpec

    spec = HuntSpec(hunt_id="noop", interval_seconds=60)
    worker = HuntWorker(ch_client, coord)
    with pytest.raises(EmptyHuntQuery):
        worker.run(spec, scheduled_start=500)
    assert coord.get_watermark("noop") is None


def test_daemon_loop_fires_a_loaded_hunt_once_against_live_ch(ch_client, dfe_db, tmp_path):
    """The WHOLE v1 runner end to end on live CH: materialise -> run_loop -> tick ->
    due -> claim -> execute -> watermark, and never-double-run on the next tick.

    Deterministic via a FIXED injected clock: interval 2s + hash offset 0 means the
    hunt's fire == the fixed 'now', so it is due on tick 1; tick 2 sees the watermark
    at that fire and does NOT re-run. One shared connection, so no multi-node split.
    """
    ch_client.command(
        f"CREATE TABLE `{dfe_db}`.src (timestamp_load Int64, msg String) "
        "ENGINE = MergeTree ORDER BY timestamp_load"
    )
    ch_client.command(
        f"CREATE TABLE `{dfe_db}`.tgt (timestamp_load Int64, msg String) "
        "ENGINE = MergeTree ORDER BY timestamp_load"
    )
    fixed_now = 1_000_000_000  # even -> fire == fixed_now for interval 2, offset 0
    ch_client.insert(
        f"{dfe_db}.src",
        [[fixed_now - 1, "in-window"], [fixed_now - 5, "too-old"]],
        column_names=["timestamp_load", "msg"],
    )

    hunt_yaml = (
        'schedule:\n  mode: rate\n  interval: "2s"\n'
        f'query: "INSERT INTO `{dfe_db}`.tgt SELECT timestamp_load, msg '
        f'FROM `{dfe_db}`.src WHERE {{window}}"\n'
        'timestamp_field: "timestamp_load"\n'
    )
    (tmp_path / "live_hunt.yaml").write_text(hunt_yaml)
    specs = load_specs(tmp_path)
    assert specs["live_hunt"].interval_seconds == 2

    coord = ChCoordinator(ch_client, database=dfe_db, settle_seconds=0.0, sleep=lambda _s: None)
    coord.ensure_schema()
    # materialise the KEDA schedule + confirm the hunt is due before we run.
    publish_schedule(ch_client, dfe_db, specs)

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
    rows = ch_client.query(f"SELECT msg FROM `{dfe_db}`.tgt").result_rows
    assert [r[0] for r in rows] == ["in-window"]
    # The watermark advanced to the fire, so tick 2 correctly did NOT re-run.
    assert coord.get_watermark("live_hunt") == fixed_now
