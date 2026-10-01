#  Project:      dfe-engine
#  File:         tests/integration/test_schema/test_source_landing_ddl_ch.py
#  Purpose:      A source deploy's generated landing DDL is applied to a real ClickHouse
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Apply the DDL a source deploy generates to a live server, and check it lands.

The manifest bootstrap has its own integration test; the per-source path did not,
and that is the half that broke. A column's declared ``index`` is rendered into
the CREATE verbatim, so a tokenizer the server has retired is a string this
generator will happily emit and only a real server will reject -- every new
source add on the default profile failing at deploy, with the landing table never
created.

Nothing here restates a literal from the schema tree. The DDL is generated the
way a deploy generates it, the server is asked to run it, and the server is then
asked what it created.
"""

import re
import uuid

import pytest

from dfe_engine.schema.engine_resolver import EngineResolver, parse_engine
from dfe_engine.schema.schema_builder_v2 import SchemaBuilderV2
from dfe_engine.schema.schema_ddl import DDLConfig, DDLGenerator
from dfe_engine.source.deployment import deploy_statements_for_build
from dfe_engine.source.models import SchemaColumn, Source
from dfe_engine.source.type_registry import TypeRegistry

pytestmark = pytest.mark.integration


@pytest.fixture
def source_database(ch_client):
    """A throwaway database for one deploy, dropped afterwards."""
    from dfe_engine.schema.applier import SchemaApplier

    db = f"dfe_source_ddl_{uuid.uuid4().hex[:8]}"
    resolver = EngineResolver(client=ch_client)
    SchemaApplier(ch_client, resolver).ensure_database(db)
    try:
        yield db
    finally:
        try:
            # The same ON CLUSTER the applier created with, or a cluster target
            # keeps the database on every node but the one this connection reached.
            on_cluster = resolver.resolve(parse_engine("MergeTree"), db).on_cluster
            ch_client.command(f"DROP DATABASE IF EXISTS `{db}`{on_cluster} SYNC")
        except Exception:
            pass


def _source(name: str) -> Source:
    """A source on the default profile -- the shape a first source add has."""
    return Source.model_validate(
        {
            "source": name,
            "match": {"field": "tags.collector.type", "value": name},
            "header": {"type": "timeseries", "version": "1.0.0"},
            "schema": {"ttl_days": 90, "engine": ""},
        }
    )


def _deploy(ch_client, db: str, source: Source) -> list[str]:
    """Generate this source's deploy DDL and run it, returning the statements."""
    builder = SchemaBuilderV2(resolver=EngineResolver(client=ch_client))
    result = builder.build(source)
    assert not result.validation_errors, result.validation_errors

    statements, _ = deploy_statements_for_build(
        builder,
        source,
        source.runtime_version_id(),
        result,
        db=db,
        ch_client=ch_client,
    )
    assert statements, "the deploy generated no DDL"
    for statement in statements:
        ch_client.command(statement)
    return statements


def _columns(client, db: str, table: str) -> set[str]:
    rows = client.query(
        "SELECT name FROM system.columns WHERE database = {db:String} AND table = {tbl:String}",
        parameters={"db": db, "tbl": table},
    ).result_rows
    return {str(row[0]) for row in rows}


def _indexes(client, db: str, table: str) -> dict[str, str]:
    """Index name to the full type the SERVER reports for it."""
    rows = client.query(
        "SELECT name, type_full FROM system.data_skipping_indices "
        "WHERE database = {db:String} AND table = {tbl:String}",
        parameters={"db": db, "tbl": table},
    ).result_rows
    return {str(row[0]): str(row[1]) for row in rows}


def test_a_source_deploy_creates_its_landing_table_on_a_live_server(ch_client, source_database):
    """The generated CREATE is DDL this ClickHouse accepts, and the table exists.

    The outage was a CREATE the server rejected outright, so the artefact to
    assert is the table on the server, not that the generator produced a string.
    """
    source = _source("landing-probe")
    _deploy(ch_client, source_database, source)

    columns = _columns(ch_client, source_database, source.table_name)
    assert columns, f"{source.table_name} was not created"
    # The profile's own columns, which is what makes it the landing shape.
    assert {"_timestamp", "_timestamp_load", "_org_id", "_raw"} <= columns


def test_the_declared_raw_index_survives_onto_the_server(ch_client, source_database):
    """The ``_raw`` text index is created, with the tokenizer the server accepts.

    The tokenizer is read back out of the generated DDL rather than written here:
    this asserts the server agrees with whatever dfe-schemas declares, so a future
    retirement fails on the mismatch instead of on a literal nobody updated.
    """
    source = _source("raw-index-probe")
    statements = _deploy(ch_client, source_database, source)

    declared = re.search(r"INDEX\s+idx__raw\s+.*?TYPE\s+(text\(.*?\))", statements[0])
    assert declared, f"the deploy DDL declares no _raw text index:\n{statements[0]}"
    tokenizer = re.search(r"tokenizer\s*=\s*'?([A-Za-z0-9_]+)", declared.group(1))
    assert tokenizer, f"no tokenizer in {declared.group(1)!r}"

    indexes = _indexes(ch_client, source_database, source.table_name)
    assert "idx__raw" in indexes, f"the server kept no _raw index; it has {sorted(indexes)}"
    reported = indexes["idx__raw"]
    assert reported.startswith("text"), reported
    assert tokenizer.group(1) in reported, (
        f"the server reports {reported!r} for the _raw index, which does not carry the "
        f"declared tokenizer {tokenizer.group(1)!r}"
    )


def test_re_running_the_same_deploy_ddl_leaves_the_table_as_it_was(ch_client, source_database):
    """The generated CREATE is safe to re-run, which is what makes a redeploy safe."""
    source = _source("repeat-probe")
    statements = _deploy(ch_client, source_database, source)
    before = _columns(ch_client, source_database, source.table_name)
    indexes_before = _indexes(ch_client, source_database, source.table_name)

    for statement in statements:
        ch_client.command(statement)

    assert _columns(ch_client, source_database, source.table_name) == before
    assert _indexes(ch_client, source_database, source.table_name) == indexes_before


def test_an_index_on_a_dotted_column_lands_through_the_create_and_the_alter(
    ch_client, source_database
):
    """An ECS column such as ``event.action`` names its index after itself, dot included.

    The server is asked to run both statements that carry an index name, and then
    asked which indexes it holds.
    """
    gen = DDLGenerator(TypeRegistry.default(), resolver=EngineResolver(client=ch_client))
    cfg = DDLConfig(db=source_database)
    table = "dotted_index_probe"

    created = SchemaColumn.model_validate(
        {"name": "event.action", "type": "string", "use_case": "exact_match"}
    )
    ch_client.command(gen.generate_create_table(table, [created], cfg))
    assert "idx_event.action" in _indexes(ch_client, source_database, table)

    added = SchemaColumn.model_validate(
        {"name": "event.outcome", "type": "string", "use_case": "dimension"}
    )
    ch_client.command(gen.generate_alter_add_column(table, added, cfg))
    for statement in gen.generate_alter_add_indexes(table, added, cfg):
        ch_client.command(statement)

    assert {"idx_event.action", "idx_event.outcome"} <= set(
        _indexes(ch_client, source_database, table)
    )
