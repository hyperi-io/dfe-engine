#  Project:      dfe-engine
#  File:         tests/unit/test_hunts/test_execution_record.py
#  Purpose:      A finished fire stores its error, duration and cost
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""What a finished fire writes to hunt_run 1.1.0, and that a failed run-now stays queued."""

import pytest

from dfe_engine.hunts.ch_coordinator import ChCoordinator

_COLUMNS = [
    "hunt_id",
    "fire",
    "status",
    "rows_written",
    "error",
    "duration_ms",
    "cost_window_minutes",
    "cost_estimate",
]


class _ClickHouse:
    """Records inserts. A finished fire issues no DDL and no read."""

    def __init__(self) -> None:
        self.inserts: list[tuple[list, list[str]]] = []

    def insert(self, table: str, data: list, column_names: list[str], database: str) -> None:
        self.inserts.append((data, column_names))


@pytest.mark.parametrize(
    ("status", "run_now", "error", "stored", "stored_error"),
    [
        pytest.param("completed", False, "", "completed", "", id="completed"),
        pytest.param("failed", False, "Code: 241.", "failed", "Code: 241.", id="failed"),
        pytest.param("failed", True, "Code: 241.", "requested", "Code: 241.", id="run-now-stays"),
        pytest.param("failed", True, "  ", "requested", "failed", id="run-now-keeps-an-error"),
        pytest.param("completed", True, "", "completed", "", id="run-now-served"),
    ],
)
def test_a_finished_fire_stores_its_status_error_duration_and_cost(
    status, run_now, error, stored, stored_error
):
    client = _ClickHouse()

    ChCoordinator(client, database="dfe").record_run(
        "h",
        42,
        3,
        status=status,
        error=error,
        duration_ms=1500,
        cost_window_minutes=60,
        cost_estimate='{"window_minutes": 60}',
        run_now=run_now,
    )

    assert client.inserts == [
        ([["h", 42, stored, 3, stored_error, 1500.0, 60, '{"window_minutes": 60}']], _COLUMNS)
    ]


def test_a_long_error_is_cut_to_the_limit():
    client = _ClickHouse()

    ChCoordinator(client, database="dfe").record_run("h", 42, 0, status="failed", error="x" * 5000)

    assert len(client.inserts[0][0][0][4]) == 2000


@pytest.mark.parametrize("status", ["requested", "", "running"])
def test_only_a_completed_or_failed_fire_is_finished(status):
    with pytest.raises(ValueError, match="completed or failed"):
        ChCoordinator(_ClickHouse(), database="dfe").record_run("h", 42, 0, status=status)
