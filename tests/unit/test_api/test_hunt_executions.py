#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_hunt_executions.py
#  Purpose:      The paginated list of one hunt's finished fires
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A hunt's executions are its fires. A queued run-now is not one, and the rows
the fire wrote are counted, not returned.
"""

from __future__ import annotations

import pytest
from scalo.resilience import ServiceUnavailable

from dfe_engine.hunts.rule_creation_service import CostEstimate

pytestmark = pytest.mark.usefixtures("customer_orgs", "certutil_rule")

_HUNT = {
    "name": "visible_hunt",
    "display_name": "Visible Hunt",
    "cron": "*/5 * * * *",
    "global_target_table_name": "dfe.detection",
    "global_source_table_name": "dfe.main",
    "customers": ["org_a"],
    "rules": ["certutil"],
}

_ESTIMATE = CostEstimate(
    estimated_rows=4,
    rows_in_window=10,
    projected_per_day=96,
    match_ratio=0.4,
    band="ok",
    window_minutes=60,
    duration_ms=3.0,
).model_dump_json()


class _Result:
    def __init__(self, rows: list) -> None:
        self.result_rows = rows


class _Executions:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.sql: list[str] = []

    def command(self, sql: str) -> None:
        return None

    def query(self, sql: str, parameters: dict | None = None) -> _Result:
        self.sql.append(sql)
        if self.fail:
            raise ServiceUnavailable("clickhouse is down")
        if "system.columns" in sql:
            return _Result([[1]])
        if sql.startswith("SELECT count()"):
            return _Result([[2]])
        return _Result(
            [
                (1_700_000_100, "completed", 4, "", 12.5, 60, _ESTIMATE),
                (1_700_000_000, "failed", 0, "Code: 241. memory limit", 1500.0, 60, ""),
            ]
        )


@pytest.fixture
def created_hunt(client, admin_headers):
    assert client.post("/api/v1/hunts", json=_HUNT, headers=admin_headers).status_code == 201
    try:
        yield _HUNT["name"]
    finally:
        client.delete(f"/api/v1/hunts/{_HUNT['name']}", headers=admin_headers)


def test_a_missing_hunt_has_no_executions(client, admin_headers):
    response = client.get("/api/v1/hunts/no-such-hunt/executions", headers=admin_headers)
    assert response.status_code == 404


def test_executions_list_the_fire_without_its_rows(
    client, admin_headers, created_hunt, monkeypatch
):
    from dfe_engine.api.v1 import hunts

    clickhouse = _Executions()
    monkeypatch.setattr(hunts, "_data_database_client", lambda: (clickhouse, "dfe"))

    response = client.get(f"/api/v1/hunts/{created_hunt}/executions", headers=admin_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["total_size"] == 2
    assert body["next_page_token"] is None
    finished, failed = body["items"]
    assert finished["execution_id"] == f"{created_hunt}:1700000100"
    assert finished["status"] == "completed"
    assert finished["rows_returned"] == 4
    assert finished["error"] is None
    assert finished["execution_time_ms"] == 12.5
    assert finished["cost_window_minutes"] == 60
    assert finished["cost_estimate"]["estimated_rows"] == 4
    assert finished["cost_estimate"]["window_minutes"] == 60
    assert "results" not in finished
    assert failed["execution_id"] == f"{created_hunt}:1700000000"
    assert failed["status"] == "failed"
    assert failed["rows_returned"] == 0
    assert failed["error"] == "Code: 241. memory limit"
    assert failed["execution_time_ms"] == 1500.0
    assert any("status != 'requested'" in sql for sql in clickhouse.sql)
    assert all("hunt_run" in sql or "system.columns" in sql for sql in clickhouse.sql)


def test_an_unreachable_coordination_table_is_a_503(
    client, admin_headers, created_hunt, monkeypatch
):
    from dfe_engine.api.v1 import hunts

    monkeypatch.setattr(hunts, "_data_database_client", lambda: (_Executions(fail=True), "dfe"))

    response = client.get(f"/api/v1/hunts/{created_hunt}/executions", headers=admin_headers)

    assert response.status_code == 503
