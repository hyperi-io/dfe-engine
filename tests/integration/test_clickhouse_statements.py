#  Project:      dfe-engine
#  File:         tests/integration/test_clickhouse_statements.py
#  Purpose:      Oversized schema DDL lands on a real ClickHouse, and a quoted ; never splits
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Schema DDL over ClickHouse's 256 KiB parse limit, applied the way a deploy applies it.

A wide vendor schema renders a table definition several times the server's default
``max_query_size``, which ClickHouse refuses with code 62 before it reads a column.
The schema here is generated to that width, built by the same builder a deploy
uses, and the server is asked what it created.

The wrapper's ``execute`` used to cut every script at every ``;``. A JSON key with
a semicolon in it is quoted into the sampling query as an identifier, which is the
one live caller that tripped it.
"""

import uuid

import pytest
import yaml

from dfe_engine.clickhouse.statements import CLICKHOUSE_DEFAULT_MAX_QUERY_SIZE, ddl_settings
from dfe_engine.schema.applier import SchemaApplier
from dfe_engine.schema.engine_resolver import EngineResolver, parse_engine
from dfe_engine.schema.schema_builder_v2 import SchemaBuilderV2
from dfe_engine.services.schema.json_promotion_service import _fetch_samples, qualified_table
from dfe_engine.source.deployment import deploy_statements_for_build, execute_ddl
from dfe_engine.source.models import Source

pytestmark = pytest.mark.integration

# Comfortably past the default once rendered, the way an Elastic vendor schema is.
WIDE_COLUMNS = 1500


@pytest.fixture
def database(ch_client):
    """A throwaway database, dropped afterwards on every node it was created on."""
    db = f"dfe_wide_ddl_{uuid.uuid4().hex[:8]}"
    resolver = EngineResolver(client=ch_client)
    SchemaApplier(ch_client, resolver).ensure_database(db)
    try:
        yield db
    finally:
        try:
            on_cluster = resolver.resolve(parse_engine("MergeTree"), db).on_cluster
            ch_client.command(f"DROP DATABASE IF EXISTS `{db}`{on_cluster} SYNC")
        except Exception:
            pass


def _wide_schema(path) -> str:
    """An additional-fields file as wide as the widest Elastic vendor schemas."""
    columns = [
        {
            "name": f"vendor_event_detail_{i:04d}",
            "type": "string",
            "use_case": "exact_match",
            "expr": f"@source: vendor.event.detail.field_{i:04d}",
            "comment": "Elasticsearch mapping type: keyword, carried through unchanged",
        }
        for i in range(WIDE_COLUMNS)
    ]
    document = {
        "current": "1.0.0",
        "versions": {"1.0.0": {"date": "2026-10-04", "type": "model", "columns": columns}},
    }
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return str(path)


def _wide_source(tmp_path) -> Source:
    return Source.model_validate(
        {
            "source": "wide-vendor-probe",
            "match": {"field": "data_stream.dataset", "value": "wide_vendor.event"},
            "header": {"type": "timeseries", "version": "1.0.0"},
            "schema": {"ttl_days": 30, "additional_fields": _wide_schema(tmp_path / "wide.yaml")},
        }
    )


def _deploy_statements(ch_client, manager_client, db: str, source: Source) -> list[str]:
    builder = SchemaBuilderV2(resolver=EngineResolver(client=ch_client))
    result = builder.build(source)
    assert not result.validation_errors, result.validation_errors
    statements, _ = deploy_statements_for_build(
        builder, source, source.runtime_version_id(), result, db=db, ch_client=manager_client
    )
    return statements


def _column_count(client, db: str, table: str) -> int:
    rows = client.query(
        "SELECT count() FROM system.columns WHERE database = {db:String} AND table = {tbl:String}",
        parameters={"db": db, "tbl": table},
    ).result_rows
    return int(rows[0][0])


def test_a_deploy_creates_a_table_whose_definition_is_over_the_parse_limit(
    ch_client, manager_client, database, tmp_path
):
    source = _wide_source(tmp_path)
    statements = _deploy_statements(ch_client, manager_client, database, source)
    create = statements[0]
    assert len(create.encode("utf-8")) > CLICKHOUSE_DEFAULT_MAX_QUERY_SIZE, (
        "the generated definition fits the default, so this proves nothing"
    )

    # Control: sent as it was before, the server refuses it at the parse limit.
    with pytest.raises(Exception, match="Max query size exceeded"):
        manager_client.execute(create)

    for statement in statements:
        execute_ddl(manager_client, statement)

    assert _column_count(ch_client, database, source.table_name) > WIDE_COLUMNS


def test_the_schema_applier_creates_a_table_over_the_parse_limit(
    ch_client, manager_client, database, tmp_path
):
    """The manifest and retention paths go through SchemaApplier, on a raw client."""
    source = _wide_source(tmp_path)
    create = _deploy_statements(ch_client, manager_client, database, source)[0]
    assert ddl_settings(create), "the generated definition fits the default"

    applier = SchemaApplier(ch_client, EngineResolver(client=ch_client))
    change = applier.ensure_table(database, source.table_name, [], create_ddl=create)

    assert change.action == "created"
    assert _column_count(ch_client, database, source.table_name) > WIDE_COLUMNS


def test_a_json_path_with_a_semicolon_samples_through_the_engines_client(
    ch_client, manager_client, database
):
    """JSON keys may carry a ``;``; the sampler quotes the path, and the wrapper sends it whole."""
    table = "json_semicolon_probe"
    ch_client.command(
        f"CREATE TABLE `{database}`.`{table}` (_json JSON) ENGINE = MergeTree ORDER BY tuple()"
    )
    ch_client.command(
        f"INSERT INTO `{database}`.`{table}` FORMAT JSONEachRow "
        '{"_json": {"rule;name": "brute-force"}}'
    )

    samples = _fetch_samples(manager_client, qualified_table(database, table), "rule;name", 5)

    assert samples == ["brute-force"]
