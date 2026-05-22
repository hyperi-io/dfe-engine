#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_phase4_routers.py
#  Purpose:      Tests for Phase 4 routers (discovery, sigma, schemas)
#  Language:     Python
#
#  License:      FSL-1.1-ALv2
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Tests for Phase 4: discovery, sigma, and schemas routers.

These tests exercise the router-level behaviour without real ClickHouse.
All CH-dependent endpoints return 503 (not configured) by design — the
routers guard against missing infrastructure cleanly.
"""

from __future__ import annotations

from fastapi.testclient import TestClient


class TestDiscoveryRouter:
    """GET /api/v1/discovery endpoints."""

    def test_databases_no_clickhouse_returns_503(self, client, admin_headers):
        """Returns 503 when ClickHouse is not reachable."""
        resp = client.get("/api/v1/discovery/databases", headers=admin_headers)
        assert resp.status_code == 503
        assert resp.json()["code"] in ("not_configured", "connection_error")

    def test_tables_no_clickhouse_returns_503(self, client, admin_headers):
        resp = client.get("/api/v1/discovery/tables", headers=admin_headers)
        assert resp.status_code == 503

    def test_columns_no_clickhouse_returns_503(self, client, admin_headers):
        resp = client.get(
            "/api/v1/discovery/tables/my_table/columns",
            headers=admin_headers,
        )
        assert resp.status_code == 503

    def test_requires_auth(self, client):
        resp = client.get("/api/v1/discovery/databases")
        assert resp.status_code == 401


class TestSigmaRouter:
    """GET/POST /api/v1/sigma endpoints."""

    def test_mappings_source_not_found(self, client, admin_headers):
        """Returns 503 or 404 when source not configured/found."""
        resp = client.get("/api/v1/sigma/mappings/nonexistent", headers=admin_headers)
        # 503 if SourceRegistry not available for sigma, or 404 if source missing
        assert resp.status_code in (404, 503)

    def test_generate_view_source_not_found(self, client, admin_headers):
        resp = client.post("/api/v1/sigma/views/nonexistent", headers=admin_headers)
        assert resp.status_code in (404, 503)

    def test_generate_all_views(self, client, admin_headers):
        """Generate all views — may return empty list or 503."""
        resp = client.post("/api/v1/sigma/views", headers=admin_headers)
        # Either succeeds with empty list (no sources) or 503 (not configured)
        assert resp.status_code in (200, 503)

    def test_logsource_search(self, client, admin_headers):
        """Find sources for logsource — returns list (possibly empty)."""
        resp = client.get(
            "/api/v1/sigma/logsource?product=windows&category=process_creation",
            headers=admin_headers,
        )
        assert resp.status_code in (200, 503)

    def test_requires_auth(self, client):
        resp = client.get("/api/v1/sigma/mappings/test")
        assert resp.status_code == 401


class TestSchemasRouter:
    """GET/POST /api/v1/schemas endpoints."""

    def test_columns_source_not_found(self, client, admin_headers):
        """Returns 404 when source doesn't exist."""
        resp = client.get("/api/v1/schemas/nonexistent/columns", headers=admin_headers)
        assert resp.status_code == 404

    def test_build_source_not_found(self, client, admin_headers):
        """Returns 404 when source doesn't exist."""
        resp = client.post("/api/v1/schemas/nonexistent/build", headers=admin_headers)
        assert resp.status_code == 404

    def test_columns_with_existing_source(self, client, admin_headers, sample_source):
        """Create a source, then try to get columns (may 404 if no schema path)."""
        # First create a source
        client.post("/api/v1/sources", json=sample_source, headers=admin_headers)
        resp = client.get(
            f"/api/v1/schemas/{sample_source['source']}/columns",
            headers=admin_headers,
        )
        # Source exists but likely has no schema file → 404 (no_schema)
        assert resp.status_code in (200, 404)
        if resp.status_code == 404:
            assert resp.json()["code"] in ("not_found", "no_schema")

    def test_requires_auth(self, client):
        resp = client.get("/api/v1/schemas/test/columns")
        assert resp.status_code == 401


class TestSchemasMetaListRouter:
    """GET /api/v1/schemas — meta-schema registry listing."""

    def test_list_meta_not_configured_returns_503(self, client, admin_headers):
        resp = client.get("/api/v1/schemas", headers=admin_headers)
        assert resp.status_code == 503
        assert resp.json()["code"] == "not_configured"

    def test_list_meta_requires_auth(self, client):
        resp = client.get("/api/v1/schemas")
        assert resp.status_code == 401

    def test_list_meta_returns_pagination_and_tree(self, tmp_path):
        from dfe_engine.api.app import create_app
        from dfe_engine.api.deps import _registries, create_access_token
        from dfe_engine.settings import (
            APISettings,
            AuthSettings,
            DFESettings,
            SchemasSettings,
            ServicesSettings,
            SourceSettings,
        )
        from dfe_engine.yaml_utils import yaml_dump

        schemas_root = tmp_path / "schemas"
        (schemas_root / "aws").mkdir(parents=True)
        yaml_dump(
            {
                "current": "1",
                "description": "Trail",
                "versions": {
                    "1": {
                        "date": "2026-01-01",
                        "type": "model",
                        "summary": "init",
                        "columns": [
                            {"name": "e", "type": "string", "expr": "@source: E"},
                        ],
                    }
                },
            },
            schemas_root / "aws" / "cloudtrail.yaml",
        )

        sources_dir = tmp_path / "sources"
        sources_dir.mkdir()
        services_dir = tmp_path / "services"
        services_dir.mkdir()
        auth_dir = tmp_path / "auth"
        auth_dir.mkdir()

        settings = DFESettings(
            config_dir=str(tmp_path),
            schemas=SchemasSettings(schemas_dir=str(schemas_root)),
            source=SourceSettings(sources_dir=str(sources_dir)),
            services=ServicesSettings(config_yaml_dir=str(services_dir)),
            auth=AuthSettings(enabled=True, auth_dir=str(auth_dir)),
            api=APISettings(jwt_secret="test-secret-key-for-unit-tests-phase4-schemas"),
        )
        app = create_app(settings)

        token = create_access_token(
            data={"sub": "admin", "org_id": "test-org", "roles": ["admin"]},
            settings=settings,
        )
        headers = {"Authorization": f"Bearer {token}"}

        try:
            with TestClient(app, raise_server_exceptions=False) as tc:
                resp = tc.get("/api/v1/schemas?page=1&per_page=10", headers=headers)
            assert resp.status_code == 200
            body = resp.json()
            assert body["total"] == 1
            assert len(body["items"]) == 1
            assert body["items"][0]["name"] == "aws/cloudtrail"
            assert body["schema_objects"]["children"]["aws"]["schemas"]
        finally:
            _registries.clear()

    def test_get_meta_schema_returns_full_definition(self, tmp_path):
        from dfe_engine.api.app import create_app
        from dfe_engine.api.deps import _registries, create_access_token
        from dfe_engine.settings import (
            APISettings,
            AuthSettings,
            DFESettings,
            SchemasSettings,
            ServicesSettings,
            SourceSettings,
        )
        from dfe_engine.yaml_utils import yaml_dump

        schemas_root = tmp_path / "schemas"
        (schemas_root / "aws").mkdir(parents=True)
        yaml_dump(
            {
                "current": "1",
                "description": "Trail",
                "versions": {
                    "1": {
                        "date": "2026-01-01",
                        "type": "model",
                        "summary": "init",
                        "columns": [
                            {"name": "e", "type": "string", "expr": "@source: E"},
                        ],
                    }
                },
            },
            schemas_root / "aws" / "cloudtrail.yaml",
        )

        sources_dir = tmp_path / "sources"
        sources_dir.mkdir()
        services_dir = tmp_path / "services"
        services_dir.mkdir()
        auth_dir = tmp_path / "auth"
        auth_dir.mkdir()

        settings = DFESettings(
            config_dir=str(tmp_path),
            schemas=SchemasSettings(schemas_dir=str(schemas_root)),
            source=SourceSettings(sources_dir=str(sources_dir)),
            services=ServicesSettings(config_yaml_dir=str(services_dir)),
            auth=AuthSettings(enabled=True, auth_dir=str(auth_dir)),
            api=APISettings(jwt_secret="test-secret-key-for-unit-tests-phase4-schemas"),
        )
        app = create_app(settings)

        token = create_access_token(
            data={"sub": "admin", "org_id": "test-org", "roles": ["admin"]},
            settings=settings,
        )
        headers = {"Authorization": f"Bearer {token}"}

        try:
            with TestClient(app, raise_server_exceptions=False) as tc:
                resp = tc.get(
                    "/api/v1/schemas/definitions/aws/cloudtrail",
                    headers=headers,
                )
                assert resp.status_code == 200
                body = resp.json()
                assert body["path"] == "aws/cloudtrail"
                assert body["current"] == "1"
                assert body["description"] == "Trail"
                assert "1" in body["versions"]
                assert body["versions"]["1"]["columns"][0]["name"] == "e"

                missing = tc.get(
                    "/api/v1/schemas/definitions/aws/missing",
                    headers=headers,
                )
                assert missing.status_code == 404
                assert missing.json()["code"] == "not_found"
        finally:
            _registries.clear()

    def test_get_meta_schema_requires_auth(self, client):
        resp = client.get("/api/v1/schemas/definitions/aws/cloudtrail")
        assert resp.status_code == 401

    def test_get_meta_not_configured_returns_503(self, client, admin_headers):
        resp = client.get(
            "/api/v1/schemas/definitions/aws/cloudtrail",
            headers=admin_headers,
        )
        assert resp.status_code == 503
        assert resp.json()["code"] == "not_configured"


class TestSchemasMetaWriteRouter:
    """POST/DELETE /api/v1/schemas/definitions/... — meta-schema registry writes."""

    @staticmethod
    def _minimal_schema_body(description: str = "new"):
        return {
            "current": "1",
            "description": description,
            "versions": {
                "1": {
                    "date": "2026-01-01",
                    "type": "model",
                    "summary": "init",
                    "columns": [{"name": "e", "type": "string", "expr": "@source: E"}],
                }
            },
        }

    def test_create_delete_round_trip(self, tmp_path):
        from dfe_engine.api.app import create_app
        from dfe_engine.api.deps import _registries, create_access_token
        from dfe_engine.settings import (
            APISettings,
            AuthSettings,
            DFESettings,
            SchemasSettings,
            ServicesSettings,
            SourceSettings,
        )

        schemas_root = tmp_path / "schemas"
        schemas_root.mkdir()
        sources_dir = tmp_path / "sources"
        sources_dir.mkdir()
        services_dir = tmp_path / "services"
        services_dir.mkdir()
        auth_dir = tmp_path / "auth"
        auth_dir.mkdir()

        settings = DFESettings(
            config_dir=str(tmp_path),
            schemas=SchemasSettings(schemas_dir=str(schemas_root)),
            source=SourceSettings(sources_dir=str(sources_dir)),
            services=ServicesSettings(config_yaml_dir=str(services_dir)),
            auth=AuthSettings(enabled=True, auth_dir=str(auth_dir)),
            api=APISettings(jwt_secret="test-secret-key-for-unit-tests-phase4-schemas-write"),
        )
        app = create_app(settings)

        token = create_access_token(
            data={"sub": "admin", "org_id": "test-org", "roles": ["admin"]},
            settings=settings,
        )
        headers = {"Authorization": f"Bearer {token}"}

        url = "/api/v1/schemas/definitions/gcp/audit_log"
        try:
            with TestClient(app, raise_server_exceptions=False) as tc:
                post = tc.post(url, json=self._minimal_schema_body(), headers=headers)
                assert post.status_code == 200
                assert post.json()["path"] == "gcp/audit_log"
                assert (schemas_root / "gcp" / "audit_log.yaml").is_file()

                duplicate = tc.post(
                    url,
                    json=self._minimal_schema_body(description="duplicate"),
                    headers=headers,
                )
                assert duplicate.status_code == 422
                assert duplicate.json()["code"] == "validation_error"

                bad = tc.post(
                    url,
                    json={**self._minimal_schema_body(), "path": "other/path"},
                    headers=headers,
                )
                assert bad.status_code == 422
                assert bad.json()["code"] == "path_mismatch"

                deleted = tc.delete(url, headers=headers)
                assert deleted.status_code == 204
                gone = tc.get(url, headers=headers)
                assert gone.status_code == 404

                missing_del = tc.delete(url, headers=headers)
                assert missing_del.status_code == 404
        finally:
            _registries.clear()

    def test_create_forbidden_for_readonly_role(self, tmp_path):
        from dfe_engine.api.app import create_app
        from dfe_engine.api.deps import _registries, create_access_token
        from dfe_engine.settings import (
            APISettings,
            AuthSettings,
            DFESettings,
            SchemasSettings,
            ServicesSettings,
            SourceSettings,
        )

        schemas_root = tmp_path / "schemas"
        schemas_root.mkdir()
        for d in ("sources", "services", "auth"):
            (tmp_path / d).mkdir()

        settings = DFESettings(
            config_dir=str(tmp_path),
            schemas=SchemasSettings(schemas_dir=str(schemas_root)),
            source=SourceSettings(sources_dir=str(tmp_path / "sources")),
            services=ServicesSettings(config_yaml_dir=str(tmp_path / "services")),
            auth=AuthSettings(enabled=True, auth_dir=str(tmp_path / "auth")),
            api=APISettings(jwt_secret="test-secret-key-for-unit-tests-phase4-schemas-write"),
        )
        app = create_app(settings)

        token = create_access_token(
            data={
                "sub": "viewer",
                "org_id": "test-org",
                "roles": ["data_analyst_viewer"],
            },
            settings=settings,
        )
        headers = {"Authorization": f"Bearer {token}"}

        try:
            with TestClient(app, raise_server_exceptions=False) as tc:
                resp = tc.post(
                    "/api/v1/schemas/definitions/x/y",
                    json=self._minimal_schema_body(),
                    headers=headers,
                )
                assert resp.status_code == 403
        finally:
            _registries.clear()

    def test_write_endpoints_require_auth(self, client):
        body = TestSchemasMetaWriteRouter._minimal_schema_body()
        resp = client.post("/api/v1/schemas/definitions/a/b", json=body)
        assert resp.status_code == 401
