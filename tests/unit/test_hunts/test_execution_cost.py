#  Project:      dfe-engine
#  File:         tests/unit/test_hunts/test_execution_cost.py
#  Purpose:      A hunt fire's alert-volume estimate
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The estimate stored on a fire is the preview of its compiled rules, read-only."""

from types import SimpleNamespace

import pytest
from clickhouse_connect.driver.exceptions import DatabaseError

from dfe_engine.hunts.execution_cost import measure_hunt_cost
from dfe_engine.hunts.hunt_spec import HuntSpec, HuntStatement
from dfe_engine.hunts.rule_creation_service import CostEstimate
from dfe_engine.settings import DetectionGuardSettings


class _ClickHouse:
    def __init__(self, rows: list[tuple[int, int] | Exception]) -> None:
        self._rows = rows
        self.queries: list[tuple[str, dict | None]] = []

    def query(self, sql: str, settings: dict | None = None) -> SimpleNamespace:
        self.queries.append((sql, settings))
        row = self._rows[len(self.queries) - 1]
        if isinstance(row, Exception):
            raise row
        return SimpleNamespace(result_rows=[row])


def test_a_compiled_rule_is_estimated_over_the_preview_window():
    client = _ClickHouse([(10, 2)])
    spec = HuntSpec(
        hunt_id="h",
        interval_seconds=60,
        tenant_ids=["acme"],
        queries=[
            HuntStatement(
                sql="INSERT INTO dfe.detection SELECT 1",
                source=("dfe", "main"),
                where_clause="severity = 1",
            )
        ],
    )

    window, raw = measure_hunt_cost(client, spec, DetectionGuardSettings())

    estimate = CostEstimate.model_validate_json(raw)
    assert window == 60
    assert estimate.window_minutes == 60
    assert estimate.estimated_rows == 2
    assert estimate.rows_in_window == 10
    assert client.queries[0][1] is not None
    assert client.queries[0][1]["readonly"] == 1
    assert "'acme'" in client.queries[0][0]


def test_two_rules_on_one_source_count_its_events_once():
    client = _ClickHouse([(10, 2), (10, 3)])
    spec = HuntSpec(
        hunt_id="h",
        interval_seconds=60,
        tenant_ids=["acme"],
        queries=[
            HuntStatement(sql="a", source=("dfe", "main"), where_clause="a = 1"),
            HuntStatement(sql="b", source=("dfe", "main"), where_clause="b = 1"),
        ],
    )

    _window, raw = measure_hunt_cost(client, spec, DetectionGuardSettings())

    estimate = CostEstimate.model_validate_json(raw)
    assert estimate.estimated_rows == 5
    assert estimate.rows_in_window == 10


def test_a_direct_query_is_not_estimated_and_reads_nothing():
    client = _ClickHouse([])
    spec = HuntSpec(hunt_id="h", interval_seconds=60, queries=[HuntStatement(sql="SELECT 1")])

    window, raw = measure_hunt_cost(client, spec, DetectionGuardSettings())

    estimate = CostEstimate.model_validate_json(raw)
    assert window == 60
    assert estimate.estimated_rows is None
    assert estimate.band == "unmeasured"
    assert client.queries == []


_RULE = HuntStatement(sql="a", source=("dfe", "main"), where_clause="a = 1")
_DRIVER_ERROR = DatabaseError(
    "Received ClickHouse exception, code: 62, server response: Code: 62. DB::Exception: "
    "Syntax error (for url http://clickhouse.internal:8123)"
)


@pytest.mark.parametrize(
    ("tenant_ids", "rows", "queried", "warning"),
    [
        pytest.param([], [], 0, "the hunt reads no org", id="no-fence-counts-nothing"),
        pytest.param(["acme"], [_DRIVER_ERROR], 1, "ClickHouse refused it", id="refused"),
    ],
)
def test_an_unmeasured_fire_says_why_without_the_server_url(tenant_ids, rows, queried, warning):
    client = _ClickHouse(rows)
    spec = HuntSpec(hunt_id="h", interval_seconds=60, tenant_ids=tenant_ids, queries=[_RULE])

    _window, raw = measure_hunt_cost(client, spec, DetectionGuardSettings())

    estimate = CostEstimate.model_validate_json(raw)
    assert (len(client.queries), estimate.band, estimate.estimated_rows) == (
        queried,
        "unmeasured",
        None,
    )
    assert warning in " ".join(estimate.warnings)
    assert "clickhouse.internal" not in raw
