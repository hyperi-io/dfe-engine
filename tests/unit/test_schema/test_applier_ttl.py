#  Project:      dfe-engine
#  File:         tests/unit/test_schema/test_applier_ttl.py
#  Purpose:      SchemaApplier reconciles a table's TTL, not only its columns
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The table-exists path of ``ensure_table`` brings the live TTL to the declared one.

A fake client stands in for ClickHouse: it answers the ``system.*`` reads the
applier makes and records every statement it is told to run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from dfe_engine.schema.applier import SchemaApplier
from dfe_engine.schema.engine_resolver import EngineResolver
from dfe_engine.schema.schema_ddl import DDLConfig
from dfe_engine.source.models import SchemaColumn

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
    """A live table with the given columns and TTL; every statement is recorded."""

    columns: list[str]
    live_ttl_days: int | None
    statements: list[str] = field(default_factory=list)

    def query(self, sql: str, parameters: dict[str, Any] | None = None) -> _Result:
        if "engine_full" in sql:
            return _Result([[_engine_full(self.live_ttl_days)]])
        if "system.columns" in sql:
            return _Result([(name,) for name in self.columns])
        return _Result([[1]])

    def command(self, sql: str) -> None:
        self.statements.append(sql)


def _columns() -> list[SchemaColumn]:
    return [
        SchemaColumn(name="_timestamp_load", type="timestamp", order=0),
        SchemaColumn(name="message", type="text"),
    ]


def _apply(client: _FakeClient, *, wanted: int | None, dry_run: bool = False):
    applier = SchemaApplier(client, EngineResolver(override="single"), dry_run=dry_run)
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


def test_a_shorter_ttl_is_still_applied():
    client = _FakeClient(columns=["_timestamp_load", "message"], live_ttl_days=90)

    _, change = _apply(client, wanted=30)

    assert len(_modify_ttl_statements(client.statements)) == 1
    assert change.ttl == "90 -> 30"


def test_a_ttl_over_an_absent_column_is_skipped_not_raised():
    client = _FakeClient(columns=["_timestamp_load", "message"], live_ttl_days=None)
    applier = SchemaApplier(client, EngineResolver(override="single"))

    change = applier.ensure_table(
        DB,
        TABLE,
        _columns(),
        DDLConfig(db=DB, ttl_days=90, ttl_columns=["gone"], projection_order_by=None),
    )

    assert client.statements == []
    assert change.action == "unchanged"


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
    ("engine_full", "expected"),
    [
        (_engine_full(30), 30),
        ("MergeTree ORDER BY x TTL x + INTERVAL 7 DAY SETTINGS index_granularity = 8192", 7),
        (_engine_full(None), None),
        ("MergeTree ORDER BY x TTL x + toIntervalHour(6) SETTINGS index_granularity = 8192", None),
    ],
)
def test_live_ttl_parsing(engine_full: str, expected: int | None):
    class _Client:
        def query(self, sql: str, parameters: dict[str, Any] | None = None) -> _Result:
            return _Result([[engine_full]])

    applier = SchemaApplier(_Client(), EngineResolver(override="single"))

    assert applier._table_ttl_days(DB, TABLE) == expected
