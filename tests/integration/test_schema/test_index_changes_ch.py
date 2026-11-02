#  Project:      dfe-engine
#  File:         tests/integration/test_schema/test_index_changes_ch.py
#  Purpose:      A redeploy adds, changes and drops the indexes of columns the table already has
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""``index_changes`` against a real ClickHouse's ``system.data_skipping_indices``.

The deploy planner skips a column the table already carries, so this is the only
path by which a derived field's index can follow its definition.
"""

import pytest
from dfe_schemas.clickhouse import PlatformProbe

from dfe_engine.schema.schema_builder_v2 import SchemaBuilderV2
from dfe_engine.schema.schema_ddl import DDLConfig
from dfe_engine.source.deployment import (
    deploy_statements_for_build,
    index_changes,
    qualify_ddl_statements,
)
from dfe_engine.source.models import SchemaColumn, Source

pytestmark = pytest.mark.integration


def _live(ch_client, db: str) -> dict[str, str]:
    rows = ch_client.query(
        "SELECT name, type_full FROM system.data_skipping_indices "
        "WHERE database = {db:String} AND table = 'events'",
        parameters={"db": db},
    ).result_rows
    return {name: kind for name, kind in rows}


def test_index_changes_bring_existing_columns_to_their_declared_indexes(
    ch_client, clickhouse_test_database
):
    db = clickhouse_test_database
    ddl = SchemaBuilderV2().ddl_generator
    # The planner renders for the placeholder and fills the database in afterwards.
    cfg = DDLConfig()
    ch_client.command(
        f"CREATE TABLE `{db}`.events (host String, user String, port UInt16, "
        "INDEX idx_host host TYPE bloom_filter GRANULARITY 4, "
        "INDEX idx_port port TYPE minmax GRANULARITY 1) "
        "ENGINE = MergeTree ORDER BY tuple()"
    )
    columns = [
        SchemaColumn(name="host", type="string"),
        SchemaColumn(name="user", type="string", use_case="exact_match"),
        SchemaColumn(name="port", type="integer", use_case="range"),
    ]
    expected = {index.name: index for col in columns for index in ddl.index_definitions(col)}

    planned = index_changes(ch_client, ddl, db, "events", columns, cfg)
    for statement in qualify_ddl_statements(planned, db):
        ch_client.command(statement)

    live = _live(ch_client, db)
    assert "idx_host" not in live
    assert set(live) == set(expected)
    # A second pass finds nothing to change, so ClickHouse's spelling of a type matches ours.
    assert index_changes(ch_client, ddl, db, "events", columns, cfg) == []


def _source(name: str) -> Source:
    return Source.model_validate(
        {
            "source": name,
            "match": {"field": "tags.collector.type", "value": name},
            "header": {"type": "timeseries", "version": "1.0.0"},
            "schema": {"ttl_days": 90, "engine": ""},
        }
    )


def _plan(manager_client, db: str, source: Source) -> list[str]:
    builder = SchemaBuilderV2(platform=PlatformProbe(manager_client).platform)
    result = builder.build(source)
    assert not result.validation_errors, result.validation_errors
    statements, _ = deploy_statements_for_build(
        builder, source, source.runtime_version_id(), result, db=db, ch_client=manager_client
    )
    return statements


def test_a_redeploy_of_an_unchanged_source_plans_no_index_change(
    ch_client, manager_client, clickhouse_test_database
):
    """Every header index, the ``_raw`` text index included, reads back as the one declared."""
    db = clickhouse_test_database
    source = _source("index-steady")
    for statement in _plan(manager_client, db, source):
        ch_client.command(statement)
    assert _live_on(ch_client, db, source.table_name)

    again = _plan(manager_client, db, source)

    assert [s for s in again if " INDEX " in s] == []


def test_a_redeploy_rebuilds_an_index_whose_granularity_moved(
    ch_client, manager_client, clickhouse_test_database
):
    db = clickhouse_test_database
    source = _source("index-moved")
    for statement in _plan(manager_client, db, source):
        ch_client.command(statement)
    live = _live_on(ch_client, db, source.table_name)
    assert live["idx__timestamp"][1] != 7
    table = f"`{db}`.`{source.table_name}`"
    ch_client.command(f"ALTER TABLE {table} DROP INDEX idx__timestamp")
    ch_client.command(
        f"ALTER TABLE {table} ADD INDEX idx__timestamp _timestamp TYPE minmax GRANULARITY 7"
    )

    planned = _plan(manager_client, db, source)
    for statement in planned:
        ch_client.command(statement)

    assert _live_on(ch_client, db, source.table_name) == live
    assert [s for s in _plan(manager_client, db, source) if " INDEX " in s] == []


def _live_on(ch_client, db: str, table: str) -> dict[str, tuple[str, int]]:
    rows = ch_client.query(
        "SELECT name, type_full, granularity FROM system.data_skipping_indices "
        "WHERE database = {db:String} AND table = {tbl:String}",
        parameters={"db": db, "tbl": table},
    ).result_rows
    return {str(name): (str(kind), int(granularity)) for name, kind, granularity in rows}
