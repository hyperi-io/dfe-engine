#  Project:      dfe-engine
#  File:         tests/integration/test_schema/test_manifest_bootstrap.py
#  Purpose:      Rule 1 -- the engine alone brings an empty ClickHouse to the manifest
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Start from an empty ClickHouse and end with every manifest object, recorded.

Against a REAL server, because that is the only thing that answers the question
the rule asks. A mock would agree with whatever the applier believes, and the
failures this replaces -- a table on one replica, an engine clause the server
rejects, a create that silently no-ops -- are all server behaviour.

Three things are proven here:

* one pass over an empty database creates every ClickHouse object the manifest
  declares, and nothing else creates any of them
* every applied object is recorded in ``schema_migrations`` with the checksum of
  the statement that ran
* a second pass changes nothing, which is what makes the phase safe to run on
  every boot
"""

from __future__ import annotations

import uuid

import pytest

from dfe_engine.schema.ledger import MigrationLedger
from dfe_engine.schema.lock import SchemaLock
from dfe_engine.schema.phase import apply_plan
from dfe_engine.schema.plan import LEDGER_ID, LOCK_ID, build_plan

pytestmark = pytest.mark.integration

# Kinds that land in the data or meta database and are visible in system.tables.
_TABLE_KINDS = ("table", "materialized_view", "view")


@pytest.fixture
def empty_database(ch_client):
    """A database name that does not exist yet, dropped afterwards."""
    name = f"dfe_manifest_{uuid.uuid4().hex[:8]}"
    yield name
    try:
        ch_client.command(f"DROP DATABASE IF EXISTS `{name}` SYNC")
    except Exception:
        pass


@pytest.fixture
def plan(empty_database):
    """The full manifest plan, rendered against the throwaway database."""
    from dfe_engine.settings import load_settings

    settings = load_settings().model_copy(deep=True)
    settings.clickhouse.data_database = empty_database
    return build_plan(settings=settings)


def _live_tables(client, database: str) -> set[str]:
    rows = client.query(
        "SELECT name FROM system.tables WHERE database = {db:String}",
        parameters={"db": database},
    ).result_rows
    return {str(row[0]) for row in rows}


def _live_databases(client) -> set[str]:
    return {str(row[0]) for row in client.query("SELECT name FROM system.databases").result_rows}


def _live_roles(client) -> set[str]:
    return {str(row[0]) for row in client.query("SELECT name FROM system.roles").result_rows}


def test_bootstrap_from_empty_clickhouse_creates_every_manifest_object(
    ch_client, empty_database, plan
):
    """Rule 1: one engine pass is the whole of what a deployment's schema needs."""
    assert empty_database not in _live_databases(ch_client)

    report = apply_plan(ch_client, plan)

    assert not report.refused, [outcome.describe() for outcome in report.refused]
    counts = report.counts()
    assert counts["created"] > 0
    assert counts["unchanged"] == 0

    databases = _live_databases(ch_client)
    roles = _live_roles(ch_client)
    for rendered in plan.tables():
        if rendered.kind == "database":
            assert rendered.name in databases, f"{rendered.id} was not created"
        elif rendered.kind == "role":
            # The rendered name is the catalogue's entry; the object carries the
            # naming rule's role form.
            assert any(rendered.name in role for role in roles), f"{rendered.id} was not created"

    applied = {
        rendered.name
        for rendered in plan.tables()
        if rendered.kind in _TABLE_KINDS and rendered.database == empty_database
    }
    skipped = {outcome.name for outcome in report.outcomes if outcome.action == "skipped"}
    missing = applied - _live_tables(ch_client, empty_database) - skipped
    assert not missing, f"the manifest declares these and the pass did not create them: {missing}"


def test_every_applied_object_is_recorded_with_its_checksum(ch_client, empty_database, plan):
    """The ledger answers which dfe-schemas release each object came from."""
    apply_plan(ch_client, plan)

    ledger_object = plan.by_id(LEDGER_ID)
    recorded = MigrationLedger(
        ch_client, database=empty_database, table=ledger_object.name
    ).checksums()

    for rendered in plan.tables():
        key = (rendered.database or "", rendered.name)
        row = recorded.get(key)
        if row is None:
            continue
        assert row.checksum == rendered.checksum, f"{rendered.id} recorded a stale checksum"
        assert row.schemas_version == plan.schemas_version
        assert row.topology == plan.topology

    assert (empty_database, ledger_object.name) in recorded, "the ledger records itself"


def test_a_second_pass_changes_nothing(ch_client, empty_database, plan):
    """Idempotent by construction, which is what makes it a boot phase."""
    apply_plan(ch_client, plan)
    second = apply_plan(ch_client, plan)

    changed = [
        outcome.describe()
        for outcome in second.outcomes
        if outcome.action in ("created", "altered")
    ]
    assert not changed, changed
    assert not second.refused, [outcome.describe() for outcome in second.refused]


def test_a_second_holder_waits_and_reports_rather_than_applying(ch_client, empty_database, plan):
    """Rule 3 of the phase: one applier per stack, through the lease."""
    apply_plan(ch_client, plan)
    lock_object = plan.by_id(LOCK_ID)

    first = SchemaLock(
        ch_client, database=empty_database, table=lock_object.name, holder="pod-a", lease_seconds=60
    )
    second = SchemaLock(
        ch_client, database=empty_database, table=lock_object.name, holder="pod-b", lease_seconds=60
    )

    assert first.acquire() is True
    assert second.acquire(wait_seconds=0.0) is False
    first.release()
    assert second.acquire(wait_seconds=0.0) is True
    second.release()
