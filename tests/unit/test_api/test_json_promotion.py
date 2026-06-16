#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_json_promotion.py
#  Purpose:      Tests for the JSON field promotion API endpoints
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Endpoint tests for ``/schemas/{source}/json-paths`` and ``/promote-field``.

Promotions here pass an explicit ``data_type`` so ClickHouse discovery is
skipped entirely -- the ClickHouse dependency is overridden with a guard client
that fails if any query is issued. The discovery SQL itself is covered by the
testcontainers integration test (no mocked database behaviour here).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dfe_engine.api.app import create_app
from dfe_engine.api.deps import _registries, create_access_token, get_clickhouse_client
from dfe_engine.settings import (
    APISettings,
    AuthSettings,
    DFESettings,
    SchemasSettings,
    ServicesSettings,
    SourceSettings,
)
from dfe_engine.yaml_utils import yaml_dump

PROMO_SOURCE = "promo_source"
NOMETA_SOURCE = "nometa_source"
SCHEMA_PATH = "meta/promo"


class _NoCallClient:
    """ClickHouse stand-in that fails loudly if a query is attempted."""

    def execute(self, *args: object, **kwargs: object):
        raise AssertionError("ClickHouse must not be queried on an explicit-data_type path")


def make_api_settings(tmp_path: Path) -> DFESettings:
    """DFESettings wired with a seeded meta-schema and two sources."""
    schemas_root = tmp_path / "schemas"
    (schemas_root / "meta").mkdir(parents=True)
    yaml_dump(
        {
            "current": "1.0.0",
            "versions": {
                "1.0.0": {
                    "date": "2026-01-01",
                    "type": "model",
                    "summary": "init",
                    "columns": [
                        {"name": "_json", "type": "json", "expr": "@captured: raw_payload as JSON"},
                    ],
                }
            },
        },
        schemas_root / "meta" / "promo.yaml",
    )

    sources_dir = tmp_path / "sources"
    sources_dir.mkdir()
    yaml_dump(
        {
            "source": PROMO_SOURCE,
            "display_name": "Promo Source",
            "enabled": True,
            "header": {"type": "time_series", "version": "1.0.0"},
            "schema_config": {"meta_schema": "meta/promo.yaml", "engine": "MergeTree"},
        },
        sources_dir / f"{PROMO_SOURCE}.yaml",
    )
    yaml_dump(
        {
            "source": NOMETA_SOURCE,
            "display_name": "No Meta Source",
            "enabled": True,
            "header": {"type": "time_series", "version": "1.0.0"},
            "schema_config": {"engine": "MergeTree"},
        },
        sources_dir / f"{NOMETA_SOURCE}.yaml",
    )

    services_dir = tmp_path / "services"
    services_dir.mkdir()
    auth_dir = tmp_path / "auth"
    auth_dir.mkdir()

    return DFESettings(
        config_dir=str(tmp_path),
        schemas=SchemasSettings(schemas_dir=str(schemas_root)),
        source=SourceSettings(sources_dir=str(sources_dir)),
        services=ServicesSettings(config_yaml_dir=str(services_dir)),
        auth=AuthSettings(enabled=True, auth_dir=str(auth_dir)),
        api=APISettings(jwt_secret="test-secret-key-for-json-promotion"),
    )


@pytest.fixture
def settings(tmp_path: Path) -> DFESettings:
    return make_api_settings(tmp_path)


@pytest.fixture
def app(settings: DFESettings):
    application = create_app(settings)
    application.dependency_overrides[get_clickhouse_client] = lambda: _NoCallClient()
    try:
        yield application
    finally:
        _registries.clear()


@pytest.fixture
def client(app) -> TestClient:
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def admin_headers(settings: DFESettings) -> dict[str, str]:
    token = create_access_token(
        data={"sub": "admin", "org_id": "test-org", "roles": ["admin"]},
        settings=settings,
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def viewer_headers(settings: DFESettings) -> dict[str, str]:
    token = create_access_token(
        data={"sub": "viewer", "org_id": "test-org", "roles": ["infra_viewer"]},
        settings=settings,
    )
    return {"Authorization": f"Bearer {token}"}


def _promote(client: TestClient, headers: dict[str, str], body: dict, **params):
    return client.post(
        f"/api/v1/schemas/{PROMO_SOURCE}/promote-field",
        json=body,
        headers=headers,
        params=params,
    )


def _schema_versions(client: TestClient, headers: dict[str, str], version: str) -> list[str]:
    resp = client.get(
        f"/api/v1/schemas/definitions/{SCHEMA_PATH}/versions/columns",
        params={"version": version},
        headers=headers,
    )
    return resp.json()["versions"]


class TestDiscoverJsonPaths:
    def test_requires_auth(self, client: TestClient):
        resp = client.get(f"/api/v1/schemas/{PROMO_SOURCE}/json-paths")
        assert resp.status_code == 401

    def test_unknown_source_404(self, client: TestClient, admin_headers):
        resp = client.get("/api/v1/schemas/ghost/json-paths", headers=admin_headers)
        assert resp.status_code == 404

    def test_source_without_meta_schema_422(self, client: TestClient, admin_headers):
        resp = client.get(f"/api/v1/schemas/{NOMETA_SOURCE}/json-paths", headers=admin_headers)
        assert resp.status_code == 422
        assert resp.json()["code"] == "no_meta_schema"


class TestPromoteField:
    def test_requires_auth(self, client: TestClient):
        resp = _promote(client, {}, {"json_path": "user.email", "data_type": "string"})
        assert resp.status_code == 401

    def test_viewer_forbidden(self, client: TestClient, viewer_headers):
        resp = _promote(client, viewer_headers, {"json_path": "user.email", "data_type": "string"})
        assert resp.status_code == 403

    def test_unknown_source_404(self, client: TestClient, admin_headers):
        resp = client.post(
            "/api/v1/schemas/ghost/promote-field",
            json={"json_path": "user.email", "data_type": "string"},
            headers=admin_headers,
        )
        assert resp.status_code == 404

    def test_source_without_meta_schema_422(self, client: TestClient, admin_headers):
        resp = client.post(
            f"/api/v1/schemas/{NOMETA_SOURCE}/promote-field",
            json={"json_path": "user.email", "data_type": "string"},
            headers=admin_headers,
        )
        assert resp.status_code == 422
        assert resp.json()["code"] == "no_meta_schema"

    def test_single_commit_creates_new_version(self, client: TestClient, admin_headers):
        resp = _promote(client, admin_headers, {"json_path": "user.email", "data_type": "string"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["schema_version"] == "1.1.0"
        assert body["results"][0]["status"] == "ok"
        assert body["results"][0]["column_name"] == "user_email"
        assert body["results"][0]["copy_cel"] == "_json.user.email"
        assert "1.1.0" in _schema_versions(client, admin_headers, "1.1.0")

    def test_committed_column_has_copy_directive(self, client: TestClient, admin_headers):
        _promote(client, admin_headers, {"json_path": "user.email", "data_type": "string"})
        resp = client.get(
            f"/api/v1/schemas/definitions/{SCHEMA_PATH}/versions/columns",
            params={"version": "1.1.0", "per_page": -1},
            headers=admin_headers,
        )
        columns = {c["name"]: c for c in resp.json()["version"]["columns"]["items"]}
        assert columns["user_email"]["expr"] == "@copy: _json.user.email"
        assert columns["user_email"]["type"] == "string"

    def test_dry_run_does_not_commit(self, client: TestClient, admin_headers):
        resp = _promote(
            client,
            admin_headers,
            {"json_path": "user.email", "data_type": "string", "index_type": "bloom_filter"},
            dry_run=True,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["schema_version"] is None
        assert body["diff"]["copy_directives"] == ["@copy: _json.user.email"]
        assert any("ADD COLUMN" in stmt for stmt in body["diff"]["ddl"])
        # No new version was written.
        assert _schema_versions(client, admin_headers, "1.0.0") == ["1.0.0"]

    def test_atomic_failure_commits_nothing(self, client: TestClient, admin_headers):
        # Promote once so user.email is already promoted.
        _promote(client, admin_headers, {"json_path": "user.email", "data_type": "string"})
        # Batch re-promote: one fresh path + the already-promoted one, atomic.
        resp = _promote(
            client,
            admin_headers,
            {"json_path": ["user.id", "user.email"], "data_type": "string", "atomic": True},
        )
        assert resp.status_code == 422
        body = resp.json()
        assert body["code"] == "promotion_failed"
        statuses = {r["json_path"]: r["status"] for r in body["context"]["results"]}
        assert statuses == {"user.id": "ok", "user.email": "error"}
        # Still only the two versions from the first promotion.
        assert _schema_versions(client, admin_headers, "1.1.0") == ["1.0.0", "1.1.0"]

    def test_best_effort_commits_ok_subset(self, client: TestClient, admin_headers):
        _promote(client, admin_headers, {"json_path": "user.email", "data_type": "string"})
        resp = _promote(
            client,
            admin_headers,
            {"json_path": ["user.id", "user.email"], "data_type": "string", "atomic": False},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["schema_version"] == "1.2.0"
        statuses = {r["json_path"]: r["status"] for r in body["results"]}
        assert statuses == {"user.id": "ok", "user.email": "error"}

    def test_batch_rejects_column_name(self, client: TestClient, admin_headers):
        resp = _promote(
            client,
            admin_headers,
            {"json_path": ["a.b"], "column_name": "x", "data_type": "string"},
        )
        assert resp.status_code == 422
