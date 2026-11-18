#  Project:      dfe-engine
#  File:         tests/integration/test_schema/test_column_comment_drift_ch.py
#  Purpose:      A changed column COMMENT reaches an existing table, on real ClickHouse
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Column comments carry the loader's ``@`` directives, so a stale one changes how rows are read.

The source-table plan and the manifest applier both compare each kept column's live
``system.columns.comment`` with the declared one and apply a difference with
``ALTER TABLE ... COMMENT COLUMN``. A second pass finds nothing to change.
"""

import uuid
from dataclasses import replace
from importlib.metadata import version

import pytest
from dfe_schemas.clickhouse import PlatformProbe
from dfe_schemas.render import RenderedObject

from dfe_engine.clickhouse.quoting import quote_literal
from dfe_engine.schema.applier import ManifestApplier, SchemaApplier, plan_source_table

pytestmark = pytest.mark.integration

STALE = "@source: timestamp | now()"
CURRENT = "@source: timestamp | @timestamp | now()"
AWKWARD = "it's a \\ back\\slash\nwith 'quotes' and a \\' end and {braces}"


def _column(name: str, comment: str | None) -> str:
    suffix = "" if comment is None else f" COMMENT {quote_literal(comment)}"
    return f"`{name}` String{suffix}"


def _create(db: str, comment: str | None, *, table: str = "events") -> str:
    return (
        f"CREATE TABLE `{db}`.`{table}` ({_column('_timestamp', comment)}, `host` String) "
        "ENGINE = MergeTree ORDER BY tuple()"
    )


def _live(ch_client, db: str, table: str = "events") -> str:
    rows = ch_client.query(
        "SELECT comment FROM system.columns WHERE database = {db:String} "
        "AND table = {tbl:String} AND name = '_timestamp'",
        parameters={"db": db, "tbl": table},
    ).result_rows
    return rows[0][0]


CASES = [
    pytest.param(STALE, STALE, id="unchanged"),
    pytest.param(STALE, CURRENT, id="changed"),
    pytest.param(None, CURRENT, id="added"),
    pytest.param(STALE, None, id="removed"),
    pytest.param(STALE, AWKWARD, id="quote-backslash-newline-brace"),
    pytest.param(AWKWARD, STALE, id="awkward-replaced"),
]


@pytest.mark.parametrize(("live", "declared"), CASES)
def test_source_table_plan_moves_a_changed_comment(
    ch_client, clickhouse_test_database, live, declared
):
    db = clickhouse_test_database
    ch_client.command(_create(db, live))
    create = _create(db, declared)

    def plan() -> tuple[str, ...]:
        return plan_source_table(
            ch_client, create, database=db, table="events", ttl_days=None
        ).statements

    statements = plan()
    for statement in statements:
        ch_client.command(statement)

    assert _live(ch_client, db) == (declared or "")
    assert bool(statements) is (live != declared)
    assert plan() == ()


def test_the_alter_is_a_comment_column_with_the_text_quoted(ch_client, clickhouse_test_database):
    db = clickhouse_test_database
    ch_client.command(_create(db, STALE))

    plan = plan_source_table(
        ch_client, _create(db, CURRENT), database=db, table="events", ttl_days=None
    )

    assert plan.statements == (
        f"ALTER TABLE `{db}`.`events` COMMENT COLUMN `_timestamp` {quote_literal(CURRENT)}",
    )
    assert [move.column for move in plan.comments] == ["_timestamp"]


def test_ensure_table_names_the_column_and_a_second_pass_changes_nothing(
    ch_client, clickhouse_test_database
):
    db = clickhouse_test_database
    ch_client.command(_create(db, STALE))
    applier = SchemaApplier(ch_client, PlatformProbe(ch_client).platform)

    first = applier.ensure_table(db, "events", [], create_ddl=_create(db, CURRENT))
    second = applier.ensure_table(db, "events", [], create_ddl=_create(db, CURRENT))

    assert (first.action, first.comments) == ("altered", ("_timestamp",))
    assert "comment on 1 column(s) [_timestamp]" in first.describe()
    assert (second.action, second.comments) == ("unchanged", ())
    assert _live(ch_client, db) == CURRENT


def _rendered(db: str, comment: str | None) -> RenderedObject:
    statement = _create(db, comment)
    return RenderedObject(
        id="test.events",
        kind="table",
        database=db,
        name="events",
        platform="self_hosted",
        additive=True,
        optional=False,
        statements=(statement,),
        checksum=uuid.uuid4().hex,
    )


@pytest.mark.parametrize(("live", "declared"), CASES)
def test_manifest_applier_moves_a_changed_comment(
    ch_client, clickhouse_test_database, live, declared
):
    db = clickhouse_test_database
    ch_client.command(_create(db, live))
    applier = ManifestApplier(
        ch_client, PlatformProbe(ch_client).platform, schemas_version=version("dfe-schemas")
    )

    first = applier.apply(_rendered(db, declared))
    second = applier.apply(_rendered(db, declared))

    assert _live(ch_client, db) == (declared or "")
    assert first.action == ("unchanged" if live == declared else "altered")
    assert first.comments == (() if live == declared else ("_timestamp",))
    assert (second.action, second.comments) == ("unchanged", ())


def test_a_table_that_refuses_the_alter_does_not_fail_the_apply(ch_client):
    """A Buffer table whose destination is itself refuses the ALTER (code 269), so the comment stays."""
    db = f"dfe_plain_{uuid.uuid4().hex[:8]}"
    ch_client.command(f"CREATE DATABASE `{db}`")
    engine = f"ENGINE = Buffer(`{db}`, `events`, 1, 1, 1, 1, 1, 1, 1)"
    try:
        ch_client.command(
            f"CREATE TABLE `{db}`.`events` ({_column('_timestamp', STALE)}, `host` String) {engine}"
        )
        applier = ManifestApplier(
            ch_client, PlatformProbe(ch_client).platform, schemas_version=version("dfe-schemas")
        )
        declared = _create(db, CURRENT).replace("ENGINE = MergeTree ORDER BY tuple()", engine)

        outcome = applier.apply(replace(_rendered(db, CURRENT), statements=(declared,)))

        assert (outcome.action, outcome.comments) == ("unchanged", ())
        assert _live(ch_client, db) == STALE
    finally:
        ch_client.command(f"DROP DATABASE IF EXISTS `{db}` SYNC")
