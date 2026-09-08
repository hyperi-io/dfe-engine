#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_system_retention.py
#  Purpose:      GET/PUT /api/v1/system/retention - the console default-TTL contract
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The override is stored in the deploy repo through gitcrud, the effective value
follows it, and a PUT reconciles the live tables against a fake ClickHouse.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from dfe_engine.api.v1.system import get_clickhouse_connector
from dfe_engine.gitcrud import GitCrud, default_registry
from dfe_engine.gitops.repo import GitopsRepo
from dfe_engine.governance import PolicyStore


@dataclass
class _Result:
    result_rows: list


@dataclass
class _FakeClient:
    """An empty server: every read answers nothing, every statement is recorded."""

    statements: list[str] = field(default_factory=list)

    def query(self, sql: str, parameters: dict[str, Any] | None = None) -> _Result:
        return _Result([])

    def command(self, sql: str) -> None:
        self.statements.append(sql)


def _wire(app, tmp_path, ch: Any = None) -> GitCrud:
    repo = GitopsRepo(local_path=str(tmp_path / "deploy"), push=False)
    gc = GitCrud(repo, default_registry())
    app.state.gitcrud = gc
    app.state.policy_store = PolicyStore(gc)
    fake = ch if ch is not None else _FakeClient()
    app.dependency_overrides[get_clickhouse_connector] = lambda: lambda: fake
    return gc


class TestGetRetention:
    def test_deployment_origin_without_an_override(self, client, app, admin_headers, tmp_path):
        _wire(app, tmp_path)
        resp = client.get("/api/v1/system/retention", headers=admin_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json() == {
            "stored": None,
            "effective": 90,
            "origin": "deployment",
            "deployment_default": 90,
        }

    def test_answers_without_gitops(self, client, admin_headers):
        resp = client.get("/api/v1/system/retention", headers=admin_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["origin"] == "deployment"

    def test_viewer_cannot_read(self, client, app, viewer_headers, tmp_path):
        _wire(app, tmp_path)
        assert client.get("/api/v1/system/retention", headers=viewer_headers).status_code == 403


class TestPutRetention:
    def test_put_stores_and_reconciles(self, client, app, admin_headers, tmp_path):
        fake = _FakeClient()
        gc = _wire(app, tmp_path, fake)

        resp = client.put(
            "/api/v1/system/retention", json={"default_ttl_days": 30}, headers=admin_headers
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["stored"] == 30
        assert body["effective"] == 30
        assert body["origin"] == "override"
        assert body["deployment_default"] == 90

        # The override is a commit in the deploy repo, read back through the app's gitcrud.
        assert gc.get("gov_settings", "retention")["default_ttl_days"] == 30
        assert gc.head_revision() is not None

        # The reconcile ran against ClickHouse with the NEW default in the DDL.
        landing = [s for s in fake.statements if "CREATE TABLE IF NOT EXISTS `dfe`.`main`" in s]
        assert len(landing) == 1
        assert "INTERVAL 30 DAY" in landing[0]
        assert "table(s) created" in body["reconcile"]["summary"]
        assert body["reconcile"]["sources_reconciled"] == 0
        assert body["reconcile"]["sources_skipped"] == 0

        got = client.get("/api/v1/system/retention", headers=admin_headers).json()
        assert got["origin"] == "override"
        assert got["effective"] == 30

    def test_put_null_clears(self, client, app, admin_headers, tmp_path):
        gc = _wire(app, tmp_path)
        client.put("/api/v1/system/retention", json={"default_ttl_days": 30}, headers=admin_headers)

        resp = client.put(
            "/api/v1/system/retention", json={"default_ttl_days": None}, headers=admin_headers
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["stored"] is None
        assert resp.json()["origin"] == "deployment"
        assert resp.json()["effective"] == 90
        assert "default_ttl_days" not in gc.get("gov_settings", "retention")

    def test_negative_is_422(self, client, app, admin_headers, tmp_path):
        _wire(app, tmp_path)
        resp = client.put(
            "/api/v1/system/retention", json={"default_ttl_days": -1}, headers=admin_headers
        )
        assert resp.status_code == 422

    def test_viewer_cannot_write(self, client, app, viewer_headers, tmp_path):
        gc = _wire(app, tmp_path)
        resp = client.put(
            "/api/v1/system/retention", json={"default_ttl_days": 30}, headers=viewer_headers
        )
        assert resp.status_code == 403
        assert gc.head_revision() is None

    def test_503_without_gitops(self, client, app, admin_headers):
        app.dependency_overrides[get_clickhouse_connector] = lambda: lambda: _FakeClient()
        resp = client.put(
            "/api/v1/system/retention", json={"default_ttl_days": 30}, headers=admin_headers
        )
        assert resp.status_code == 503
        assert resp.json()["code"] == "not_configured"

    def test_clickhouse_failure_is_502_with_the_override_stored(
        self, client, app, admin_headers, tmp_path
    ):
        gc = _wire(app, tmp_path)

        def _unreachable():
            raise ConnectionError("clickhouse down")

        app.dependency_overrides[get_clickhouse_connector] = lambda: _unreachable
        resp = client.put(
            "/api/v1/system/retention", json={"default_ttl_days": 30}, headers=admin_headers
        )
        assert resp.status_code == 502, resp.text
        assert resp.json()["code"] == "reconcile_failed"
        assert gc.get("gov_settings", "retention")["default_ttl_days"] == 30
