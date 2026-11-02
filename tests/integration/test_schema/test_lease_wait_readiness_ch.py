#  Project:      dfe-engine
#  File:         tests/integration/test_schema/test_lease_wait_readiness_ch.py
#  Purpose:      A boot pass that gives up waiting on another applier's lease is not ready
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Readiness against a real ClickHouse while another applier holds the schema lease.

The database carries the lease table and nothing else, the state a second replica
meets while the first is still applying, so a pass reported converged here is
ready over a schema that does not exist.
"""

import functools
import uuid

import pytest
from dfe_schemas.clickhouse import PlatformProbe, create_database

from dfe_engine.clickhouse.bootstrap import SchemaRetry
from dfe_engine.clickhouse.clickhouse_manager import ClickHouseManager
from dfe_engine.schema import phase
from dfe_engine.schema.lock import SchemaLock
from dfe_engine.schema.plan import LEDGER_ID, LOCK_ID, render_one
from dfe_engine.settings import ClickHouseResilienceSettings, DFESettings
from tests.support.clickhouse_cleanup import drop_database_and_policies

pytestmark = [pytest.mark.integration, pytest.mark.own_clickhouse]

FAST = ClickHouseResilienceSettings(wait_initial=0.001, wait_max=0.001)


@pytest.fixture
def local_ch(ch_params):
    if ch_params["host"] != "127.0.0.1":
        pytest.skip("applies the whole manifest, so it runs only on a throwaway local ClickHouse")
    return ch_params


@pytest.fixture
def lease_only(ch_client, local_ch):
    """A data database holding only the lease table, dropped afterwards."""
    database = f"dfe_leasewait_{uuid.uuid4().hex[:8]}"
    ch_client.command(create_database(database, PlatformProbe(ch_client).platform))
    for statement in render_one(LOCK_ID, data_database=database).statements:
        ch_client.command(statement)
    ClickHouseManager.reset_instance()
    try:
        yield database
    finally:
        ClickHouseManager.reset_instance()
        phase._set_state(phase.SchemaBootstrapState())
        drop_database_and_policies(ch_client, database)


def _settings(ch: dict, database: str) -> DFESettings:
    return DFESettings(
        env="dev",
        clickhouse={
            "host": ch["host"],
            "port": ch["port"],
            "username": ch["username"],
            "password": ch["password"],
            "secure": False,
            "data_database": database,
            "bootstrap_tables": True,
            "bootstrap_wait_seconds": 0,
        },
        transport={"default": "direct", "bus_present": False},
    )


def _table_exists(client, database: str, table: str) -> bool:
    rows = client.query(
        "SELECT 1 FROM system.tables WHERE database = {db:String} AND name = {tbl:String}",
        parameters={"db": database, "tbl": table},
    ).result_rows
    return bool(rows)


def test_a_pass_that_outwaits_a_live_lease_stays_not_ready_until_it_applies(
    ch_client, local_ch, lease_only
):
    database = lease_only
    ledger = render_one(LEDGER_ID, data_database=database).name
    holder = SchemaLock(
        ch_client,
        database=database,
        table=render_one(LOCK_ID, data_database=database).name,
        holder="pod-a",
        lease_seconds=300,
    )
    # A claim held up by a server stall is a missed attempt, so taking the lease here waits.
    assert holder.acquire(wait_seconds=10.0) is True
    run_pass = functools.partial(
        phase.run_bootstrap, settings=_settings(local_ch, database), wait_seconds=1.0
    )

    state = run_pass()

    assert state.state == phase.STATE_OBSERVED
    assert state.holder == "pod-a"
    assert state.converged is False, "a pass that applied nothing reported the schema converged"
    assert phase.schema_ready() is False, "readiness went true while the holder was applying"
    assert state.transient is True
    assert not _table_exists(ch_client, database, ledger)

    passes: list[str] = []

    def retried_pass() -> phase.SchemaBootstrapState:
        outcome = run_pass()
        passes.append(outcome.state)
        if len(passes) == 1:
            holder.release()
        return outcome

    final = SchemaRetry(retried_pass, resilience=FAST).run()

    assert passes == [phase.STATE_OBSERVED, phase.STATE_CONVERGED]
    assert final is not None
    assert final.state == phase.STATE_CONVERGED, final.error
    assert phase.schema_ready() is True
    assert _table_exists(ch_client, database, ledger)
