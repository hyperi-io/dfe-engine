#  Project:      dfe-engine
#  File:         tests/unit/test_hunts/test_execution_record.py
#  Purpose:      A finished fire stores its error, duration and cost when the table can
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""hunt_run gained its execution columns after the first deploys, so a fire writes
them when they are there and the original four when they are not.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from dfe_engine.hunts.ch_coordinator import ChCoordinator


class _ClickHouse:
    def __init__(self, present: bool) -> None:
        self.present = present
        self.inserts: list[tuple[list, list[str]]] = []

    def command(self, sql: str) -> None:
        return None

    def query(self, sql: str, parameters: dict | None = None) -> SimpleNamespace:
        return SimpleNamespace(result_rows=[[1 if self.present else 0]])

    def insert(self, table: str, data: list, column_names: list[str], database: str) -> None:
        self.inserts.append((data, column_names))


def test_a_finished_fire_stores_its_error_duration_and_cost_when_the_columns_exist():
    client = _ClickHouse(present=True)

    ChCoordinator(client, database="dfe").record_run(
        "h",
        42,
        0,
        status="failed",
        error="Code: 241. memory limit",
        duration_ms=1500,
        cost_window_minutes=60,
        cost_estimate='{"window_minutes": 60}',
    )

    data, columns = client.inserts[0]
    assert columns == [
        "hunt_id",
        "fire",
        "status",
        "rows_written",
        "error",
        "duration_ms",
        "cost_window_minutes",
        "cost_estimate",
    ]
    assert data == [
        ["h", 42, "failed", 0, "Code: 241. memory limit", 1500.0, 60, '{"window_minutes": 60}']
    ]


def test_a_table_without_the_columns_still_records_the_fire():
    client = _ClickHouse(present=False)

    ChCoordinator(client, database="dfe").record_run("h", 42, 7)

    data, columns = client.inserts[0]
    assert columns == ["hunt_id", "fire", "status", "rows_written"]
    assert data == [["h", 42, "completed", 7]]


def test_a_queued_run_is_not_a_finished_fire():
    with pytest.raises(ValueError):
        ChCoordinator(_ClickHouse(present=False), database="dfe").record_run(
            "h", 42, 0, status="requested"
        )
