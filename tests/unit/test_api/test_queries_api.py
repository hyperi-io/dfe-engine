#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_queries_api.py
#  Purpose:      Queries API - lifespan-wired ViewExecutor + raw-query config
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Queries router over the lifespan-built ViewExecutor.

Fakes ONLY the external clickhouse-connect seams (client acquisition on the
adapter/manager); the lifespan wiring, router, catalog and executor are real.
No live ClickHouse.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dfe_engine.clickhouse.clickhouse_manager import ClickHouseManager
from dfe_engine.query.datasources.clickhouse import ClickHouseAdapter
from dfe_engine.settings import (
    APISettings,
    AuthSettings,
    ClickHouseSettings,
    DFESettings,
    HuntsSettings,
    ServicesSettings,
    SourceSettings,
)

_VIEW_SQL = (
    "CREATE VIEW default.dfe_v_analytics_events AS "
    "SELECT id, value FROM events "
    "WHERE org_id = {org_id:String} LIMIT {limit:UInt32}"
)


@pytest.fixture
def api_settings(tmp_path: Path) -> DFESettings:
    """Override the shared fixture: adds an explicit ClickHouse config so the
    tests can assert the settings-derived values reach the connection seam."""
    for sub in ("sources", "services", "rules", "hunts", "auth"):
        (tmp_path / sub).mkdir()
    return DFESettings(
        config_dir=str(tmp_path),
        source=SourceSettings(sources_dir=str(tmp_path / "sources")),
        services=ServicesSettings(config_yaml_dir=str(tmp_path / "services")),
        hunts=HuntsSettings(rules_dir=str(tmp_path / "rules"), hunt_dir=str(tmp_path / "hunts")),
        auth=AuthSettings(enabled=True, auth_dir=str(tmp_path / "auth")),
        api=APISettings(
            jwt_secret="test-secret-key-for-unit-tests-hmac32",
            jwt_expire_minutes=30,
        ),
        clickhouse=ClickHouseSettings(
            host="ch.test", port=8123, username="svc", password="pw", secure=False
        ),
    )


class _FakeResult:
    def __init__(self, columns, rows):
        self.column_names = columns
        self.result_rows = rows


class _FakeCHClient:
    """Records queries/commands; serves system.tables for the catalog."""

    def __init__(self):
        self.queries: list[tuple] = []
        self.commands: list[str] = []

    def query(self, sql, parameters=None, settings=None, **_kw):
        self.queries.append((sql, parameters, settings))
        if "system.tables" in sql:
            return _FakeResult(
                ["name", "create_table_query", "metadata_modification_time"],
                [("dfe_v_analytics_events", _VIEW_SQL, None)],
            )
        return _FakeResult(["id", "value"], [(1, "a"), (2, "b")])

    def command(self, sql, *_a, **_kw):
        self.commands.append(sql)

    def close(self):
        pass


class _FakeManager:
    def __init__(self, client):
        self._client = client

    def get_clickhouse_client(self):
        return self._client


@pytest.fixture
def fake_ch(monkeypatch):
    """Fake the two client-acquisition seams the executor composition uses."""
    admin = _FakeCHClient()
    restricted = _FakeCHClient()
    monkeypatch.setattr(ClickHouseAdapter, "get_restricted_client", lambda self: restricted)
    manager = _FakeManager(admin)
    monkeypatch.setattr(
        ClickHouseManager, "get_instance", classmethod(lambda cls, cfg=None: manager)
    )
    return admin, restricted


def test_lifespan_wires_view_executor_and_lists_views(app, fake_ch, admin_headers):
    admin, _ = fake_ch
    with TestClient(app, raise_server_exceptions=False) as client:
        assert app.state.view_executor is not None
        r = client.get("/api/v1/queries/views", headers=admin_headers)
        assert r.status_code == 200, r.text
        labels = [v["label"] for v in r.json()]
        assert "analytics/events" in labels
        # auto_bootstrap (default True) applied the builtin views at startup
        assert any(cmd.lstrip().upper().startswith("CREATE") for cmd in admin.commands)


def test_execute_view_reaches_executor(app, fake_ch, admin_headers):
    _, restricted = fake_ch
    with TestClient(app, raise_server_exceptions=False) as client:
        r = client.post(
            "/api/v1/queries/views/analytics/events/execute",
            json={"params": {}},
            headers=admin_headers,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["rows"] == [{"id": 1, "value": "a"}, {"id": 2, "value": "b"}]
        # the parameterized view call went to the restricted client
        sql = restricted.queries[-1][0]
        assert "dfe_v_analytics_events(" in sql


def test_ch_down_degrades_views_to_503_not_startup_failure(app, admin_headers, monkeypatch):
    def _boom(self):
        raise ConnectionError("CH down")

    monkeypatch.setattr(ClickHouseAdapter, "get_restricted_client", _boom)
    with TestClient(app, raise_server_exceptions=False) as client:
        assert app.state.view_executor is None
        r = client.get("/api/v1/queries/views", headers=admin_headers)
        assert r.status_code == 503
        assert r.json()["code"] == "not_configured"  # unified ErrorResponse shape


def test_raw_query_binds_manager_to_settings_config(app, api_settings, admin_headers, monkeypatch):
    """POST /queries/raw must bind the manager singleton to DFE_CLICKHOUSE_*,
    not the hardcoded localhost defaults."""
    import clickhouse_connect

    captured: dict = {}

    class _FakeConnectClient:
        def query(self, sql, parameters=None, settings=None, **_kw):
            return _FakeResult(["n"], [(1,)])

        def command(self, sql, *_a, **_kw):
            return None

        def close(self):
            pass

    def fake_get_client(**kwargs):
        captured.clear()
        captured.update(kwargs)
        return _FakeConnectClient()

    monkeypatch.setattr(clickhouse_connect, "get_client", fake_get_client)
    with TestClient(app, raise_server_exceptions=False) as client:
        # Drop whatever the lifespan bound so the raw endpoint is what binds.
        ClickHouseManager.reset_instance()
        captured.clear()
        try:
            r = client.post(
                "/api/v1/queries/raw",
                json={"datasource": "clickhouse:default", "query": "SELECT 1"},
                headers=admin_headers,
            )
            assert r.status_code == 200, r.text
            assert captured["host"] == "ch.test"
            assert captured["port"] == 8123
            assert captured["username"] == "svc"
        finally:
            ClickHouseManager.reset_instance()
