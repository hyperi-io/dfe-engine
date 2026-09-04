#  Project:      dfe-engine
#  File:         tests/integration/test_clickhouse_resilience.py
#  Purpose:      CH manager resilient path against a REAL ClickHouse
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The resilient CH manager exercised against a REAL ClickHouse (no mocks).

Uses the shared ``ch_params`` fixture (tiered: cluster -> remote -> local docker;
skips if none reachable), so every op here runs through scalo's
ReconnectingResilience against a live server: a real SELECT round-trips, a real
non-transient CH error (unknown table) surfaces immediately un-retried, and a
forced reconnect rebuilds a WORKING pooled client. No ClickHouse Cloud (billable)
is touched - auto-wake is covered by the unit doubles.

``insert`` is here for the reason it was missing in the first place: the unit tests
drive it against a recording double, so an AttributeError on the write path can only
be caught by a real server accepting real rows.
"""

from __future__ import annotations

import uuid

import pytest
from scalo.resilience import ServiceUnavailable

from dfe_engine.clickhouse.clickhouse_manager import ClickHouseManager

pytestmark = pytest.mark.integration


def _manager_cfg(ch_params: dict) -> dict:
    return {
        "ch_host": ch_params["host"],
        "ch_port": ch_params["port"],
        "ch_username": ch_params.get("username"),
        "ch_password": ch_params.get("password", ""),
        "ch_secure": ch_params.get("secure", False),
        "ch_verify": False,
    }


@pytest.fixture
def manager(ch_params):
    """A ClickHouseManager pointed at the real test CH (own instance, not the singleton)."""
    mgr = ClickHouseManager(_manager_cfg(ch_params))
    try:
        yield mgr
    finally:
        mgr._cleanup()


def test_query_round_trips_through_resilient_wrapper(manager):
    client = manager.get_clickhouse_client()
    result = client.query("SELECT 1")
    assert result.result_rows[0][0] == 1


def test_execute_scalar_through_resilient_wrapper(manager):
    client = manager.get_clickhouse_client()
    rows = client.execute("SELECT 42")
    assert rows[0][0] == 42


def test_non_transient_ch_error_surfaces_immediately(manager):
    client = manager.get_clickhouse_client()
    # A real UNKNOWN_TABLE / UNKNOWN_IDENTIFIER is a query-logic error: NOT
    # retryable, so it must surface as itself, never as ServiceUnavailable.
    with pytest.raises(Exception) as excinfo:
        client.query("SELECT * FROM __dfe_nonexistent_table_resilience_xyz")
    assert not isinstance(excinfo.value, ServiceUnavailable)


@pytest.fixture
def heartbeat_table(manager):
    """The REAL hunt_runner_heartbeat table, in a throwaway database.

    Built from ``internal_tables`` through the applier the deployment uses, so the
    insert below writes the columns and the engine a deployment actually has -- TTL
    clause included, which is DDL only a real server can accept or reject.
    """
    from dfe_engine.schema.applier import SchemaApplier
    from dfe_engine.schema.engine_resolver import EngineResolver
    from dfe_engine.schema.internal_tables import hunt_runner_heartbeat_spec

    client = manager.get_clickhouse_client()
    db = f"dfe_hb_{uuid.uuid4().hex[:8]}"
    applier = SchemaApplier(client, EngineResolver(client=client))
    applier.ensure_database(db)
    spec = hunt_runner_heartbeat_spec(db)
    applier.ensure_table(db, spec.name, spec.columns, spec.config)
    try:
        yield db, spec.name
    finally:
        client.command(f"DROP DATABASE IF EXISTS `{db}` SYNC")


def test_the_wrapper_inserts_rows_a_real_clickhouse_reads_back(manager, heartbeat_table):
    db, table = heartbeat_table
    client = manager.get_clickhouse_client()
    runner = f"runner-{uuid.uuid4().hex[:6]}"

    client.insert(
        table,
        [[runner, 1_700_000_000, 5.0]],
        column_names=["runner_id", "seen", "poll_seconds"],
        database=db,
    )

    rows = client.query(
        f"SELECT seen, poll_seconds FROM `{db}`.`{table}` WHERE runner_id = %(r)s",
        parameters={"r": runner},
    ).result_rows
    assert rows == [(1_700_000_000, 5.0)]


def test_a_real_runner_heartbeat_round_trips_through_the_wrapper(manager, heartbeat_table):
    """The API's own liveness read, over rows the coordinator's writer put there."""
    from dfe_engine.hunt_runner.run_status import live_runner_count

    db, _table = heartbeat_table
    client = manager.get_clickhouse_client()
    now = 1_700_000_000
    client.insert(
        "hunt_runner_heartbeat",
        [[f"runner-{uuid.uuid4().hex[:6]}", now, 5.0]],
        column_names=["runner_id", "seen", "poll_seconds"],
        database=db,
    )

    assert live_runner_count(client, db, now) == 1
    # Two polls past its last beat, the same runner no longer counts.
    assert live_runner_count(client, db, now + 60) == 0


def test_reconnect_rebuilds_a_working_client(manager):
    client = manager.get_clickhouse_client()
    assert client.query("SELECT 1").result_rows[0][0] == 1

    # Force the resilience reconnect hook (tears the pooled client down) ...
    manager._reconnect()
    assert manager._client is None

    # ... and prove the NEXT op rebuilds a working client against the real server.
    assert client.query("SELECT 2").result_rows[0][0] == 2
    assert manager._client is not None
