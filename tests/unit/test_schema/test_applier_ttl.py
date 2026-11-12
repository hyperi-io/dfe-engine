#  Project:      dfe-engine
#  File:         tests/unit/test_schema/test_applier_ttl.py
#  Purpose:      The applier reads CREATE and engine_full text and moves a table TTL
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The table-exists path of ``ensure_table`` brings the live TTL to the declared one.

A fake client stands in for ClickHouse: it answers the ``system.*`` reads the
applier makes and records every statement it is told to run.
"""

from dataclasses import dataclass, field
from typing import Any

import pytest
from dfe_schemas.clickhouse import Platform

from dfe_engine.schema.applier import (
    LiveTable,
    LiveTtl,
    SchemaApplier,
    SchemaApplyError,
    plan_source_table,
    read_table,
    ttl_from_engine_full,
    ttl_move,
)
from dfe_engine.schema.schema_ddl import DDLConfig, DDLGenerationError, DDLGenerator
from dfe_engine.source.models import SchemaColumn
from dfe_engine.source.type_registry import TypeRegistry

DB = "dfe"
TABLE = "main"


def _engine_full(ttl_days: int | None) -> str:
    base = (
        "MergeTree PARTITION BY toYYYYMMDD(_timestamp_load) PRIMARY KEY _timestamp_load "
        "ORDER BY _timestamp_load"
    )
    if ttl_days is not None:
        base += f" TTL _timestamp_load + toIntervalDay({ttl_days}) WHERE _timestamp_load >= 0"
    return base + " SETTINGS index_granularity = 2048, ttl_only_drop_parts = 1"


@dataclass
class _Result:
    result_rows: list


@dataclass
class _FakeClient:
    """A live table with the given columns and TTL. Every statement is recorded."""

    columns: list[str]
    live_ttl_days: int | None
    statements: list[str] = field(default_factory=list)

    def engine_full(self) -> str:
        return _engine_full(self.live_ttl_days)

    def query(self, sql: str, parameters: dict[str, Any] | None = None) -> _Result:
        if "engine_full" in sql:
            return _Result([["", "", self.engine_full()]])
        if "system.columns" in sql:
            return _Result([(name, "String") for name in self.columns])
        if "data_skipping_indices" in sql:
            return _Result([])
        return _Result([[1]])

    def command(self, sql: str, settings: dict[str, Any] | None = None) -> None:
        self.statements.append(sql)


def _columns() -> list[SchemaColumn]:
    return [
        SchemaColumn(name="_timestamp_load", type="timestamp", order=0),
        SchemaColumn(name="message", type="text"),
    ]


def _definition(ttl_days: int):
    """The CREATE a table keeping *ttl_days* renders, read."""
    create = DDLGenerator(TypeRegistry.default()).generate_create_table(
        TABLE, _columns(), DDLConfig(db=DB, ttl_days=ttl_days, projection_order_by=None)
    )
    return read_table(create)


def _apply(client: _FakeClient, *, wanted: int | None, dry_run: bool = False):
    applier = SchemaApplier(client, Platform.SELF_HOSTED, dry_run=dry_run)
    change = applier.ensure_table(
        DB, TABLE, _columns(), DDLConfig(db=DB, ttl_days=wanted, projection_order_by=None)
    )
    return applier, change


def _modify_ttl_statements(statements: list[str]) -> list[str]:
    return [s for s in statements if s.startswith("ALTER TABLE") and "MODIFY TTL" in s]


def test_a_longer_declared_ttl_is_applied_with_one_modify_ttl():
    client = _FakeClient(columns=["_timestamp_load", "message"], live_ttl_days=30)

    _, change = _apply(client, wanted=90)

    alters = _modify_ttl_statements(client.statements)
    assert len(alters) == 1
    assert alters[0].startswith(f"ALTER TABLE `{DB}`.`{TABLE}` MODIFY TTL _timestamp_load")
    assert "INTERVAL 90 DAY" in alters[0]
    assert client.statements == alters
    assert change.action == "altered"
    assert change.ttl == "30 -> 90"
    assert change.columns_added == ()
    assert "TTL 30 -> 90 days" in change.describe()


def test_an_equal_ttl_is_left_alone():
    client = _FakeClient(columns=["_timestamp_load", "message"], live_ttl_days=90)

    _, change = _apply(client, wanted=90)

    assert client.statements == []
    assert change.action == "unchanged"
    assert change.ttl == ""


def test_a_table_with_no_ttl_gains_the_declared_one():
    client = _FakeClient(columns=["_timestamp_load", "message"], live_ttl_days=None)

    _, change = _apply(client, wanted=90)

    assert len(_modify_ttl_statements(client.statements)) == 1
    assert change.action == "altered"
    assert change.ttl == "none -> 90"


def test_an_undeclared_ttl_never_removes_the_live_one():
    client = _FakeClient(columns=["_timestamp_load", "message"], live_ttl_days=30)

    _, change = _apply(client, wanted=None)

    assert client.statements == []
    assert change.action == "unchanged"
    assert change.ttl == ""


@pytest.mark.parametrize("live_ttl_days", [30, 0])
def test_a_declared_zero_removes_the_live_ttl(live_ttl_days: int):
    client = _FakeClient(columns=["_timestamp_load", "message"], live_ttl_days=live_ttl_days)

    _, change = _apply(client, wanted=0)

    assert client.statements == [f"ALTER TABLE `{DB}`.`{TABLE}` REMOVE TTL"]
    assert change.action == "altered"
    assert change.ttl == f"{live_ttl_days} -> none"


def test_a_declared_zero_leaves_a_table_with_no_ttl_alone():
    client = _FakeClient(columns=["_timestamp_load", "message"], live_ttl_days=None)

    _, change = _apply(client, wanted=0)

    assert client.statements == []
    assert change.action == "unchanged"
    assert change.ttl == ""


def test_a_shorter_ttl_is_still_applied():
    client = _FakeClient(columns=["_timestamp_load", "message"], live_ttl_days=90)

    _, change = _apply(client, wanted=30)

    assert len(_modify_ttl_statements(client.statements)) == 1
    assert change.ttl == "90 -> 30"


def test_a_ttl_over_an_absent_column_is_refused_before_anything_runs():
    client = _FakeClient(columns=["_timestamp_load", "message"], live_ttl_days=None)
    applier = SchemaApplier(client, Platform.SELF_HOSTED)
    cfg = DDLConfig(db=DB, ttl_days=90, ttl_columns=["gone"], projection_order_by=None)

    with pytest.raises(DDLGenerationError, match="gone"):
        applier.ensure_table(DB, TABLE, _columns(), cfg)
    assert client.statements == []


@pytest.mark.parametrize(
    ("engine", "variant"),
    [
        ("MergeTree", "MergeTree"),
        ("ReplicatedMergeTree", "MergeTree"),
        ("SharedReplacingMergeTree", "ReplacingMergeTree"),
        ("ReplacingMergeTree", "ReplacingMergeTree"),
    ],
)
def test_a_live_engine_compares_without_its_replicated_or_shared_prefix(engine: str, variant: str):
    assert LiveTable(engine=engine, ttl=None).variant == variant


def test_a_ttl_in_hours_is_not_read_as_no_ttl_and_a_declared_zero_removes_it():
    class _HourTtlClient(_FakeClient):
        def engine_full(self) -> str:
            return "MergeTree ORDER BY x TTL x + toIntervalHour(6) SETTINGS a = 1"

    client = _HourTtlClient(columns=["_timestamp_load", "message"], live_ttl_days=None)

    _, change = _apply(client, wanted=0)

    assert client.statements == [f"ALTER TABLE `{DB}`.`{TABLE}` REMOVE TTL"]
    assert change.ttl == "6 hour -> none"


@pytest.mark.parametrize(
    ("live", "wanted", "expires"),
    [
        (LiveTtl(90, "day"), 30, True),
        (LiveTtl(30, "day"), 90, False),
        (None, 90, True),
        (LiveTtl(6, "hour"), 90, True),
        (LiveTtl(3, "month"), 400, True),
    ],
)
def test_a_move_that_deletes_rows_kept_today_is_marked(
    live: LiveTtl | None, wanted: int, expires: bool
):
    move = ttl_move(target="`dfe`.`t`", wanted=wanted, definition=_definition(wanted), live=live)

    assert "MODIFY TTL _timestamp_load + INTERVAL" in move.statement
    assert move.expires_rows is expires


def test_removing_a_ttl_never_expires_rows():
    move = ttl_move(
        target="`dfe`.`t`", wanted=0, definition=_definition(0), live=LiveTtl(30, "day")
    )

    assert move.statement == "ALTER TABLE `dfe`.`t` REMOVE TTL"
    assert move.move == "30 -> none"
    assert move.expires_rows is False


def test_dry_run_records_the_modify_ttl_without_running_it():
    client = _FakeClient(columns=["_timestamp_load", "message"], live_ttl_days=30)

    applier, change = _apply(client, wanted=90, dry_run=True)

    assert client.statements == []
    assert len(_modify_ttl_statements(applier.report.statements)) == 1
    assert change.ttl == "30 -> 90"


def test_a_missing_column_and_a_ttl_move_report_together():
    client = _FakeClient(columns=["_timestamp_load"], live_ttl_days=30)

    _, change = _apply(client, wanted=90)

    assert change.action == "altered"
    assert change.columns_added == ("message",)
    assert change.ttl == "30 -> 90"
    assert client.statements[0].startswith("ALTER TABLE")
    assert "ADD COLUMN" in client.statements[0]
    assert "MODIFY TTL" in client.statements[-1]


@pytest.mark.parametrize(
    ("engine_full", "expected", "days"),
    [
        (_engine_full(30), LiveTtl(30, "day"), 30),
        (
            "MergeTree ORDER BY x TTL x + INTERVAL 7 DAY SETTINGS index_granularity = 8192",
            LiveTtl(7, "day"),
            7,
        ),
        (_engine_full(None), None, None),
        (
            "MergeTree ORDER BY x TTL x + toIntervalHour(6) SETTINGS index_granularity = 8192",
            LiveTtl(6, "hour"),
            None,
        ),
        ("MergeTree ORDER BY x TTL x + toIntervalHour(48) SETTINGS a = 1", LiveTtl(48, "hour"), 2),
        ("MergeTree ORDER BY x TTL x + toIntervalWeek(2) SETTINGS a = 1", LiveTtl(2, "week"), 14),
        (
            "MergeTree ORDER BY x TTL x + toIntervalMonth(3) SETTINGS a = 1",
            LiveTtl(3, "month"),
            None,
        ),
    ],
)
def test_live_ttl_parsing(engine_full: str, expected: LiveTtl | None, days: int | None):
    live = ttl_from_engine_full(engine_full)

    assert live == expected
    assert (None if live is None else live.days) == days


_CREATE = (
    "CREATE TABLE IF NOT EXISTS `{db}`.`t` (\n"
    "    `a.b` LowCardinality(String) DEFAULT 'TTL' COMMENT 'it''s, SETTINGS' CODEC(Delta(8), ZSTD(1)),\n"
    "    `ts` DateTime64(3),\n"
    "    INDEX `idx_a.b` mapKeys(`a.b`) TYPE text(tokenizer = array) GRANULARITY 1\n"
    ") ENGINE = ReplicatedMergeTree ORDER BY (`ts`) TTL `ts` + INTERVAL 2 WEEK\n"
    "SETTINGS index_granularity = 2048, storage_policy = 'a,b' COMMENT 'ORDER BY x';"
)


# Bare column names that spell a clause or column keyword.
_KEYWORD_NAMES = (
    "CREATE TABLE t (`ttl` DateTime, `comment` String, `x` Tuple(default String, codec String))\n"
    "ENGINE = MergeTree ORDER BY ttl TTL ttl + INTERVAL 30 DAY DELETE WHERE ttl >= 0\n"
    "SETTINGS index_granularity = 8192"
)


@pytest.mark.parametrize(
    ("statement", "read", "expected"),
    [
        pytest.param(_CREATE, lambda d: list(d.columns), ["a.b", "ts"], id="columns_in_order"),
        pytest.param(_CREATE, lambda d: d.types["a.b"], "LowCardinality(String)", id="type_alone"),
        pytest.param(_CREATE, lambda d: d.codecs, {"a.b": ("DELTA", "ZSTD")}, id="codec_families"),
        pytest.param(_CREATE, lambda d: "a.b" in d.indexes[0].columns, True, id="index_column"),
        pytest.param(
            _CREATE, lambda d: d.indexes[0].granularity, None, id="text_index_granularity"
        ),
        pytest.param(_CREATE, lambda d: d.clauses["ORDER BY"], "(`ts`)", id="no_clause_in_strings"),
        pytest.param(_CREATE, lambda d: d.ttl.days, 14, id="ttl_in_weeks"),
        pytest.param(_CREATE, lambda d: d.settings["storage_policy"], "'a,b'", id="quoted_setting"),
        pytest.param(
            _KEYWORD_NAMES,
            lambda d: d.clauses["TTL"],
            "ttl + INTERVAL 30 DAY DELETE WHERE ttl >= 0",
            id="ttl_column_named_ttl",
        ),
        pytest.param(_KEYWORD_NAMES, lambda d: d.clauses["ORDER BY"], "ttl", id="order_by_ttl"),
        pytest.param(
            _KEYWORD_NAMES,
            lambda d: d.types["x"],
            "Tuple(default String, codec String)",
            id="tuple_fields_named_like_clauses",
        ),
        pytest.param(_KEYWORD_NAMES, lambda d: d.codecs, {}, id="no_codec_inside_a_type"),
    ],
)
def test_a_create_reads_as_it_declares(statement, read, expected):
    assert read(read_table(statement)) == expected


@pytest.mark.parametrize("statement", ["CREATE VIEW v AS SELECT 1", "", "CREATE TABLE t 'open"])
def test_a_statement_with_no_column_body_is_refused(statement: str):
    with pytest.raises(SchemaApplyError):
        read_table(statement)


@dataclass
class _IndexedClient(_FakeClient):
    """As :class:`_FakeClient`, with the indexes ``system.data_skipping_indices`` reports."""

    indexes: list[tuple[str, str, int]] = field(default_factory=list)

    def query(self, sql: str, parameters: dict[str, Any] | None = None) -> _Result:
        if "data_skipping_indices" in sql:
            return _Result(list(self.indexes))
        return super().query(sql, parameters)


@pytest.mark.parametrize(
    ("live_granularity", "moves"),
    [pytest.param(4, False, id="same_index"), pytest.param(1, True, id="granularity_changed")],
)
def test_a_backticked_index_name_matches_the_bare_name_clickhouse_reports(
    live_granularity: int, moves: bool
):
    """The CREATE backticks every index name, and system.data_skipping_indices reports it bare.

    A deployed table whose index is unchanged plans no index statement, so
    the requoting reads as no drift. A real difference still moves.
    """
    columns = [*_columns(), SchemaColumn(name="src_ip", type="ip", use_case="exact_match")]
    create = DDLGenerator(TypeRegistry.default()).generate_create_table(
        TABLE, columns, DDLConfig(db=DB, ttl_days=30, projection_order_by=None)
    )
    assert "INDEX `idx_src_ip` `src_ip` TYPE bloom_filter GRANULARITY 4" in create
    client = _IndexedClient(
        columns=[c.name for c in columns],
        live_ttl_days=30,
        indexes=[("idx_src_ip", "bloom_filter", live_granularity)],
    )

    plan = plan_source_table(client, create, database=DB, table=TABLE, ttl_days=30)

    assert any("idx_src_ip" in statement for statement in plan.statements) is moves
