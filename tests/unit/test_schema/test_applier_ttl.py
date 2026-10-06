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

from dfe_engine.schema.applier import (
    LiveTable,
    LiveTtl,
    SchemaApplier,
    ttl_from_engine_full,
    ttl_move,
)
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

    def command(self, sql: str, settings: dict[str, Any] | None = None) -> None:
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
    assert change.ttl == ""
    assert "gone" in change.ttl_skipped


def test_a_ttl_already_in_place_is_not_reported_as_skipped():
    client = _FakeClient(columns=["_timestamp_load", "message"], live_ttl_days=90)

    _, change = _apply(client, wanted=90)

    assert change.ttl_skipped == ""


@pytest.mark.parametrize(
    ("engine", "variant"),
    [
        ("MergeTree", "MergeTree"),
        ("ReplicatedMergeTree", "MergeTree"),
        ("SharedReplacingMergeTree", "ReplacingMergeTree"),
        ("ReplacingMergeTree", "ReplacingMergeTree"),
    ],
)
def test_a_live_engine_compares_without_its_topology_prefix(engine: str, variant: str):
    assert LiveTable(engine=engine, ttl=None).variant == variant


def test_a_ttl_in_hours_is_not_read_as_no_ttl_and_a_declared_zero_removes_it():
    class _HourTtlClient(_FakeClient):
        def query(self, sql: str, parameters: dict[str, Any] | None = None) -> _Result:
            if "engine_full" in sql:
                return _Result([["MergeTree ORDER BY x TTL x + toIntervalHour(6) SETTINGS a = 1"]])
            return super().query(sql, parameters)

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
    move = ttl_move(
        target="`dfe`.`t`",
        on_cluster="",
        cfg=DDLConfig(db=DB, ttl_days=wanted),
        columns=_columns(),
        live=live,
    )

    assert "MODIFY TTL _timestamp_load + INTERVAL" in move.statement
    assert move.expires_rows is expires


def test_removing_a_ttl_never_expires_rows():
    move = ttl_move(
        target="`dfe`.`t`",
        on_cluster="",
        cfg=DDLConfig(db=DB, ttl_days=0),
        columns=_columns(),
        live=LiveTtl(30, "day"),
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
