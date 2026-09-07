#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_phase4_routers.py
#  Purpose:      Tests for Phase 4 routers (discovery, sigma, schemas)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Tests for Phase 4: discovery, sigma, and schemas routers.

These tests exercise the router-level behaviour without real ClickHouse.
All CH-dependent endpoints return 503 (not configured) by design — the
routers guard against missing infrastructure cleanly.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

# These apps run the production posture, which refuses to start on the shipped
# admin password, so each injects one the way a deployment's secret store does.
ADMIN_PASSWORD = "test-admin-pw"


@pytest.fixture
def discovery_client(app, client: TestClient) -> TestClient:
    """Discovery router tests without ClickHouse (hermetic — not host-dependent)."""
    app.state.connection_registry = None
    return client


class TestDiscoveryRouter:
    """GET /api/v1/discovery endpoints."""

    def test_databases_no_clickhouse_returns_503(self, discovery_client, admin_headers):
        """Returns 503 when ClickHouse is not reachable."""
        resp = discovery_client.get("/api/v1/discovery/databases", headers=admin_headers)
        assert resp.status_code == 503
        assert resp.json()["code"] in ("not_configured", "connection_error")

    def test_tables_no_clickhouse_returns_503(self, discovery_client, admin_headers):
        resp = discovery_client.get("/api/v1/discovery/tables", headers=admin_headers)
        assert resp.status_code == 503

    def test_columns_no_clickhouse_returns_503(self, discovery_client, admin_headers):
        resp = discovery_client.get(
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
    """GET/POST /api/v1/sources/{name}/columns and /build (formerly under /schemas)."""

    def test_columns_source_not_found(self, client, admin_headers):
        """Returns 404 when source doesn't exist."""
        resp = client.get("/api/v1/sources/nonexistent/columns", headers=admin_headers)
        assert resp.status_code == 404

    def test_build_source_not_found(self, client, admin_headers):
        """Returns 404 when source doesn't exist."""
        resp = client.post("/api/v1/sources/nonexistent/build", headers=admin_headers)
        assert resp.status_code == 404

    def test_build_unknown_source_version(self, client, admin_headers, sample_source):
        client.post("/api/v1/sources", json=sample_source, headers=admin_headers)
        resp = client.post(
            f"/api/v1/sources/{sample_source['source']}/build?version=9.9.9",
            headers=admin_headers,
        )
        assert resp.status_code == 404

    def test_deploy_source_not_found(self, client, admin_headers):
        resp = client.post("/api/v1/sources/nonexistent/deploy", headers=admin_headers)
        assert resp.status_code == 404

    def test_deploy_requires_auth(self, client):
        assert client.post("/api/v1/sources/x/deploy").status_code == 401

    def test_deploy_no_schema_source_returns_404(self, client, admin_headers, sample_source):
        client.post("/api/v1/sources", json=sample_source, headers=admin_headers)
        resp = client.post(
            f"/api/v1/sources/{sample_source['source']}/deploy", headers=admin_headers
        )
        assert resp.status_code == 404
        assert resp.json()["code"] == "no_schema"

    def test_deploy_dry_run_plans_ddl_without_touching_clickhouse(self, tmp_path, monkeypatch):
        # A source WITH a real schema; dry_run must return the DDL and apply nothing
        # (no ClickHouse configured here - if plan touched CH this would 503).
        import shutil

        from dfe_engine.api.app import create_app
        from dfe_engine.api.deps import create_access_token
        from dfe_engine.schema.schema_loader import _BUNDLED_PROFILES_DIR
        from dfe_engine.settings import (
            APISettings,
            AuthSettings,
            DFESettings,
            LocalAuthSettings,
            SchemasSettings,
            ServicesSettings,
            SourceSettings,
            reset_settings,
        )
        from dfe_engine.yaml_utils import yaml_dump

        reset_settings()
        schemas_root = tmp_path / "schemas"
        schemas_root.mkdir()
        common_header = schemas_root / "common-header"
        common_header.mkdir()
        shutil.copy(_BUNDLED_PROFILES_DIR / "minimal.yaml", common_header / "minimal.yaml")
        monkeypatch.setenv("DFE_SCHEMAS_DIR", str(schemas_root))
        yaml_dump(
            {
                "current": "1.0.0",
                "versions": {
                    "1.0.0": {
                        "columns": [
                            {"name": "alpha", "type": "string", "use_case": "dimension"},
                            {"name": "beta", "type": "integer"},
                        ]
                    }
                },
            },
            schemas_root / "meta_cols.yaml",
        )
        sources_dir = tmp_path / "sources"
        sources_dir.mkdir()
        yaml_dump(
            {
                "source": "dep-src",
                "enabled": True,
                "match": {"field": "tags.collector.type", "value": "dep-src"},
                "deployed_version": "1.0.0",
                "current": "1.0.0",
                "versions": {
                    "1.0.0": {
                        "date_time": "2026-01-01",
                        "header": {"type": "minimal", "version": "1.0.0"},
                        "schema": {
                            "meta_schema": "meta_cols.yaml",
                            "meta_schema_version": "1.0.0",
                            "engine": "MergeTree",
                        },
                    }
                },
            },
            sources_dir / "dep-src.yaml",
        )
        (tmp_path / "services").mkdir()
        (tmp_path / "auth").mkdir()
        settings = DFESettings(
            config_dir=str(tmp_path),
            schemas=SchemasSettings(schemas_dir=str(schemas_root)),
            source=SourceSettings(sources_dir=str(sources_dir)),
            services=ServicesSettings(config_yaml_dir=str(tmp_path / "services")),
            auth=AuthSettings(
                enabled=True,
                auth_dir=str(tmp_path / "auth"),
                local=LocalAuthSettings(admin_password=ADMIN_PASSWORD),
            ),
            api=APISettings(jwt_secret="test-secret-key-for-unit-tests-phase4-deploy"),
        )
        app = create_app(settings)
        token = create_access_token(
            data={"sub": "admin", "org_id": "test-org", "roles": ["admin"]},
            settings=settings,
        )
        headers = {"Authorization": f"Bearer {token}"}
        try:
            with TestClient(app, raise_server_exceptions=False) as tc:
                resp = tc.post("/api/v1/sources/dep-src/deploy?dry_run=true", headers=headers)
                assert resp.status_code == 200, resp.text
                body = resp.json()
                assert body["dry_run"] is True
                assert body["applied"] is False
                assert body["statements_applied"] == 0
                assert "CREATE TABLE" in body["create_table"].upper()
        finally:
            reset_settings()

    def test_deploy_persists_deployed_version(self, tmp_path, monkeypatch):
        import shutil
        from unittest.mock import MagicMock

        from dfe_engine.api.app import create_app
        from dfe_engine.api.deps import create_access_token
        from dfe_engine.schema.schema_loader import _BUNDLED_PROFILES_DIR
        from dfe_engine.settings import (
            APISettings,
            AuthSettings,
            DFESettings,
            LocalAuthSettings,
            SchemasSettings,
            ServicesSettings,
            SourceSettings,
            reset_settings,
        )
        from dfe_engine.yaml_utils import yaml_dump, yaml_load

        reset_settings()
        schemas_root = tmp_path / "schemas"
        schemas_root.mkdir()
        common_header = schemas_root / "common-header"
        common_header.mkdir()
        shutil.copy(_BUNDLED_PROFILES_DIR / "minimal.yaml", common_header / "minimal.yaml")
        monkeypatch.setenv("DFE_SCHEMAS_DIR", str(schemas_root))
        yaml_dump(
            {
                "current": "1.0.0",
                "versions": {
                    "1.0.0": {
                        "columns": [
                            {"name": "alpha", "type": "string", "use_case": "dimension"},
                        ]
                    }
                },
            },
            schemas_root / "meta_cols.yaml",
        )
        sources_dir = tmp_path / "sources"
        sources_dir.mkdir()
        deploys_dir = tmp_path / "source-deploys"
        deploys_dir.mkdir()
        yaml_dump(
            {
                "source": "dep-src",
                "enabled": True,
                "match": {"field": "tags.collector.type", "value": "dep-src"},
                "current": "1.0.0",
                "versions": {
                    "1.0.0": {
                        "date_time": "2026-01-01",
                        "header": {"type": "minimal", "version": "1.0.0"},
                        "schema": {
                            "meta_schema": "meta_cols.yaml",
                            "meta_schema_version": "1.0.0",
                            "engine": "MergeTree",
                        },
                    }
                },
            },
            sources_dir / "dep-src.yaml",
        )
        (tmp_path / "services").mkdir()
        (tmp_path / "auth").mkdir()

        mock_ch = MagicMock()
        mock_manager = MagicMock()
        mock_manager.get_clickhouse_client.return_value = mock_ch
        monkeypatch.setattr(
            "dfe_engine.clickhouse.clickhouse_manager.ClickHouseManager.get_instance",
            lambda _cfg: mock_manager,
        )
        monkeypatch.setattr(
            "dfe_engine.services.schema.json_promotion_service.clickhouse_table_exists",
            lambda *_a, **_k: False,
        )

        settings = DFESettings(
            config_dir=str(tmp_path),
            schemas=SchemasSettings(schemas_dir=str(schemas_root)),
            source=SourceSettings(
                sources_dir=str(sources_dir),
                deploys_dir=str(deploys_dir),
            ),
            services=ServicesSettings(config_yaml_dir=str(tmp_path / "services")),
            auth=AuthSettings(
                enabled=True,
                auth_dir=str(tmp_path / "auth"),
                local=LocalAuthSettings(admin_password=ADMIN_PASSWORD),
            ),
            api=APISettings(jwt_secret="test-secret-key-for-unit-tests-phase4-deploy2"),
        )
        app = create_app(settings)
        monkeypatch.setattr(
            "dfe_engine.settings.get_settings",
            lambda: settings,
        )
        token = create_access_token(
            data={"sub": "admin", "org_id": "test-org", "roles": ["admin"]},
            settings=settings,
        )
        headers = {"Authorization": f"Bearer {token}"}
        try:
            with TestClient(app, raise_server_exceptions=False) as tc:
                resp = tc.post("/api/v1/sources/dep-src/deploy", headers=headers)
                assert resp.status_code == 200, resp.text
                assert resp.json()["applied"] is True

                detail = tc.get("/api/v1/sources/dep-src", headers=headers)
                assert detail.status_code == 200
                assert detail.json()["deployed_version"] == "1.0.0"

                deploy_doc = yaml_load(deploys_dir / "dep-src.yaml")
                assert deploy_doc["deployed_version"] == "1.0.0"
        finally:
            reset_settings()

    def test_columns_with_existing_source(self, client, admin_headers, sample_source):
        """Create a source, then try to get columns (404 when version has no schema YAML refs)."""
        client.post("/api/v1/sources", json=sample_source, headers=admin_headers)
        resp = client.get(
            f"/api/v1/sources/{sample_source['source']}/columns",
            headers=admin_headers,
        )
        assert resp.status_code == 404
        assert resp.json()["code"] == "no_schema"

    def test_columns_unknown_source_version(self, client, admin_headers, sample_source):
        client.post("/api/v1/sources", json=sample_source, headers=admin_headers)
        resp = client.get(
            f"/api/v1/sources/{sample_source['source']}/columns?version=9.9.9",
            headers=admin_headers,
        )
        assert resp.status_code == 404
        assert "9.9.9" in resp.json()["message"]

    def test_columns_invalid_per_page_returns_422(self, client, admin_headers):
        resp = client.get(
            "/api/v1/sources/nonexistent/columns?per_page=0",
            headers=admin_headers,
        )
        assert resp.status_code == 422

    def test_source_columns_paginated(self, tmp_path, monkeypatch):
        import shutil

        from dfe_engine.api.app import create_app
        from dfe_engine.api.deps import _registries, create_access_token
        from dfe_engine.schema.schema_loader import _BUNDLED_PROFILES_DIR
        from dfe_engine.settings import (
            APISettings,
            AuthSettings,
            DFESettings,
            LocalAuthSettings,
            SchemasSettings,
            ServicesSettings,
            SourceSettings,
            reset_settings,
        )
        from dfe_engine.yaml_utils import yaml_dump

        # The columns route resolves schema files via the global settings
        # singleton; reset it so this test's DFE_SCHEMAS_DIR is honoured even
        # when an earlier test has already populated the cache.
        reset_settings()

        schemas_root = tmp_path / "schemas"
        schemas_root.mkdir()
        common_header = schemas_root / "common-header"
        common_header.mkdir()
        shutil.copy(_BUNDLED_PROFILES_DIR / "minimal.yaml", common_header / "minimal.yaml")
        monkeypatch.setenv("DFE_SCHEMAS_DIR", str(schemas_root))
        yaml_dump(
            {
                "current": "1.0.0",
                "versions": {
                    "1.0.0": {
                        "columns": [
                            {"name": "alpha", "type": "string", "use_case": "dimension"},
                            {"name": "beta", "type": "integer"},
                            {"name": "gamma", "type": "text", "use_case": "fulltext"},
                        ]
                    }
                },
            },
            schemas_root / "meta_cols.yaml",
        )

        sources_dir = tmp_path / "sources"
        sources_dir.mkdir()
        yaml_dump(
            {
                "source": "cols-src",
                "enabled": True,
                "match": {"field": "tags.collector.type", "value": "cols-src"},
                "deployed_version": "1.0.0",
                "current": "1.0.0",
                "versions": {
                    "1.0.0": {
                        "date_time": "2026-01-01",
                        "header": {"type": "minimal", "version": "1.0.0"},
                        "schema": {
                            "meta_schema": "meta_cols.yaml",
                            "meta_schema_version": "1.0.0",
                            "engine": "MergeTree",
                        },
                    }
                },
            },
            sources_dir / "cols-src.yaml",
        )

        services_dir = tmp_path / "services"
        services_dir.mkdir()
        auth_dir = tmp_path / "auth"
        auth_dir.mkdir()

        settings = DFESettings(
            config_dir=str(tmp_path),
            schemas=SchemasSettings(schemas_dir=str(schemas_root)),
            source=SourceSettings(sources_dir=str(sources_dir)),
            services=ServicesSettings(config_yaml_dir=str(services_dir)),
            auth=AuthSettings(
                enabled=True,
                auth_dir=str(auth_dir),
                local=LocalAuthSettings(admin_password=ADMIN_PASSWORD),
            ),
            api=APISettings(jwt_secret="test-secret-key-for-unit-tests-phase4-source-columns"),
        )
        app = create_app(settings)

        token = create_access_token(
            data={"sub": "admin", "org_id": "test-org", "roles": ["admin"]},
            settings=settings,
        )
        headers = {"Authorization": f"Bearer {token}"}
        base_url = "/api/v1/sources/cols-src/columns"

        try:
            with TestClient(app, raise_server_exceptions=False) as tc:
                resp = tc.get(base_url, headers=headers)
                assert resp.status_code == 200
                body = resp.json()
                total = body["total"]
                assert total >= 3
                assert body["page"] == 1
                assert body["per_page"] == 25
                assert len(body["items"]) == total
                assert all(
                    key in body["items"][0]
                    for key in ("name", "type", "use_case", "attribute", "description")
                )
                # attribute matches meta-schema column shape: list[str] | null
                for item in body["items"]:
                    assert item["attribute"] is None or isinstance(item["attribute"], list)

                page2 = tc.get(f"{base_url}?page=2&per_page=2", headers=headers)
                assert page2.status_code == 200
                page2_body = page2.json()
                assert page2_body["page"] == 2
                assert page2_body["per_page"] == 2
                assert page2_body["total"] == total
                expected_page2_len = min(2, max(0, total - 2))
                assert len(page2_body["items"]) == expected_page2_len
                if total > 2:
                    assert page2_body["next_page"] == 3
                    assert page2_body["prev_page"] == 1

                all_page = tc.get(f"{base_url}?per_page=-1", headers=headers)
                assert all_page.status_code == 200
                all_body = all_page.json()
                assert all_body["per_page"] == -1
                assert len(all_body["items"]) == all_body["total"]
                names = {item["name"] for item in all_body["items"]}
                assert {"alpha", "beta", "gamma"}.issubset(names)
        finally:
            _registries.clear()
            reset_settings()

    def test_source_plan_dry_run(self, tmp_path, monkeypatch):
        import shutil

        from dfe_engine.api.app import create_app
        from dfe_engine.api.deps import _registries, create_access_token, get_clickhouse_client
        from dfe_engine.schema.schema_loader import _BUNDLED_PROFILES_DIR
        from dfe_engine.settings import (
            APISettings,
            AuthSettings,
            DFESettings,
            LocalAuthSettings,
            SchemasSettings,
            ServicesSettings,
            SourceSettings,
            reset_settings,
        )
        from dfe_engine.yaml_utils import yaml_dump

        reset_settings()

        class _FakeCh:
            def execute(self, query: str, *args, **kwargs):
                if "system.tables" in query:
                    return []
                if "system.columns" in query:
                    return []
                return []

        schemas_root = tmp_path / "schemas"
        schemas_root.mkdir()
        common_header = schemas_root / "common-header"
        common_header.mkdir()
        shutil.copy(_BUNDLED_PROFILES_DIR / "minimal.yaml", common_header / "minimal.yaml")
        monkeypatch.setenv("DFE_SCHEMAS_DIR", str(schemas_root))
        monkeypatch.setenv("DFE_CONFIG_DIR", str(tmp_path))
        sources_dir = tmp_path / "sources"
        sources_dir.mkdir()
        monkeypatch.setenv("DFE_SOURCES_DIR", str(sources_dir))
        reset_settings()
        yaml_dump(
            {
                "current": "1.0.0",
                "versions": {
                    "1.0.0": {
                        "columns": [
                            {"name": "alpha", "type": "string", "use_case": "dimension"},
                        ]
                    }
                },
            },
            schemas_root / "meta_cols.yaml",
        )

        yaml_dump(
            {
                "source": "plan-src",
                "enabled": True,
                "match": {"field": "tags.collector.type", "value": "plan-src"},
                "current": "1.0.0",
                "versions": {
                    "1.0.0": {
                        "date_time": "2026-01-01",
                        "header": {"type": "minimal", "version": "1.0.0"},
                        "schema": {
                            "meta_schema": "meta_cols.yaml",
                            "meta_schema_version": "1.0.0",
                            "engine": "MergeTree",
                        },
                    }
                },
            },
            sources_dir / "plan-src.yaml",
        )

        settings = DFESettings(
            config_dir=str(tmp_path),
            schemas=SchemasSettings(schemas_dir=str(schemas_root)),
            source=SourceSettings(sources_dir=str(sources_dir)),
            services=ServicesSettings(config_yaml_dir=str(tmp_path / "services")),
            auth=AuthSettings(
                enabled=True,
                auth_dir=str(tmp_path / "auth"),
                local=LocalAuthSettings(admin_password=ADMIN_PASSWORD),
            ),
            api=APISettings(jwt_secret="test-secret-key-for-unit-tests-phase4-plan"),
        )
        (tmp_path / "services").mkdir()
        (tmp_path / "auth").mkdir()
        app = create_app(settings)
        app.dependency_overrides[get_clickhouse_client] = lambda: _FakeCh()

        token = create_access_token(
            data={"sub": "admin", "org_id": "test-org", "roles": ["admin"]},
            settings=settings,
        )
        headers = {"Authorization": f"Bearer {token}"}

        try:
            with TestClient(app, raise_server_exceptions=False) as tc:
                plan_resp = tc.post("/api/v1/sources/plan-src/plan", headers=headers)
                assert plan_resp.status_code == 200
                body = plan_resp.json()
                assert body["source_name"] == "plan-src"
                assert body["version"] == "1.0.0"
                assert body["ready"] is True
                assert body["statements"]

                assert (tmp_path / "source-builds" / "plan-src.yaml").is_file()
                assert not (tmp_path / "source-plans" / "plan-src.yaml").exists()
        finally:
            app.dependency_overrides.clear()
            _registries.clear()
            reset_settings()

    def test_requires_auth(self, client):
        resp = client.get("/api/v1/sources/test/columns")
        assert resp.status_code == 401


class TestSchemasMetaListRouter:
    """GET /api/v1/schemas — meta-schema registry listing."""

    def test_list_meta_empty_registry_returns_200(self, client, admin_headers):
        resp = client.get("/api/v1/schemas", headers=admin_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 0
        assert body["items"] == []

    def test_list_meta_not_configured_returns_503(self, tmp_path):
        from dfe_engine.api.app import create_app
        from dfe_engine.api.deps import _registries, create_access_token
        from dfe_engine.settings import (
            APISettings,
            AuthSettings,
            DFESettings,
            LocalAuthSettings,
            SchemasSettings,
            ServicesSettings,
            SourceSettings,
        )

        sources_dir = tmp_path / "sources"
        sources_dir.mkdir()
        services_dir = tmp_path / "services"
        services_dir.mkdir()
        auth_dir = tmp_path / "auth"
        auth_dir.mkdir()

        settings = DFESettings(
            config_dir=str(tmp_path),
            schemas=SchemasSettings(schemas_dir=""),
            source=SourceSettings(sources_dir=str(sources_dir)),
            services=ServicesSettings(config_yaml_dir=str(services_dir)),
            auth=AuthSettings(
                enabled=True,
                auth_dir=str(auth_dir),
                local=LocalAuthSettings(admin_password=ADMIN_PASSWORD),
            ),
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
                resp = tc.get("/api/v1/schemas", headers=headers)
            assert resp.status_code == 503
            assert resp.json()["code"] == "not_configured"
        finally:
            _registries.clear()

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
            LocalAuthSettings,
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
            auth=AuthSettings(
                enabled=True,
                auth_dir=str(auth_dir),
                local=LocalAuthSettings(admin_password=ADMIN_PASSWORD),
            ),
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
            assert body["objects"]["children"]["aws"]["items"]
        finally:
            _registries.clear()

    def test_list_meta_filters_by_schema_type(self, tmp_path):
        from dfe_engine.api.app import create_app
        from dfe_engine.api.deps import _registries, create_access_token
        from dfe_engine.settings import (
            APISettings,
            AuthSettings,
            DFESettings,
            LocalAuthSettings,
            SchemasSettings,
            ServicesSettings,
            SourceSettings,
        )
        from dfe_engine.yaml_utils import yaml_dump

        schema_body = {
            "current": "1",
            "versions": {
                "1": {
                    "date": "2026-01-01",
                    "type": "model",
                    "summary": "init",
                    "columns": [
                        {
                            "name": "e",
                            "type": "string",
                            "expr": "@source: E",
                            "_field_type": "base",
                        }
                    ],
                }
            },
        }

        schemas_root = tmp_path / "schemas"
        (schemas_root / "aws").mkdir(parents=True)
        (schemas_root / "meta").mkdir(parents=True)
        yaml_dump(schema_body, schemas_root / "aws" / "cloudtrail.yaml")
        yaml_dump(schema_body, schemas_root / "meta" / "logs_base.yaml")

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
            auth=AuthSettings(
                enabled=True,
                auth_dir=str(auth_dir),
                local=LocalAuthSettings(admin_password=ADMIN_PASSWORD),
            ),
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
                all_resp = tc.get("/api/v1/schemas?per_page=-1", headers=headers)
                meta_resp = tc.get(
                    "/api/v1/schemas?schema_type=meta&per_page=-1",
                    headers=headers,
                )
                multi_resp = tc.get(
                    "/api/v1/schemas?schema_type=meta&schema_type=aws&per_page=-1",
                    headers=headers,
                )
                none_resp = tc.get(
                    "/api/v1/schemas?schema_type=unknown&per_page=-1",
                    headers=headers,
                )
            assert all_resp.status_code == 200
            assert all_resp.json()["total"] == 2

            assert meta_resp.status_code == 200
            meta_body = meta_resp.json()
            assert meta_body["total"] == 1
            assert meta_body["items"][0]["name"] == "meta/logs_base"
            assert set(meta_body["objects"]["children"]) == {"meta"}

            assert multi_resp.status_code == 200
            assert multi_resp.json()["total"] == 2

            assert none_resp.status_code == 200
            assert none_resp.json()["total"] == 0
            assert none_resp.json()["items"] == []
        finally:
            _registries.clear()

    def test_get_meta_schema_returns_full_definition(self, tmp_path):
        from dfe_engine.api.app import create_app
        from dfe_engine.api.deps import _registries, create_access_token
        from dfe_engine.settings import (
            APISettings,
            AuthSettings,
            DFESettings,
            LocalAuthSettings,
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
                "resource_type": "core",
                "versions": {
                    "1": {
                        "date": "2026-01-01",
                        "type": "model",
                        "summary": "init",
                        "columns": [
                            {
                                "name": "e",
                                "type": "string",
                                "expr": "@source: E",
                                "_field_type": "base",
                            },
                            {"name": "z_col", "type": "integer", "_field_type": "base"},
                            {"name": "a_col", "type": "string", "_field_type": "base"},
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
            auth=AuthSettings(
                enabled=True,
                auth_dir=str(auth_dir),
                local=LocalAuthSettings(admin_password=ADMIN_PASSWORD),
            ),
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
                columns_url = "/api/v1/schemas/definitions/aws/cloudtrail/versions/columns"
                resp = tc.get(f"{columns_url}?version=1", headers=headers)
                assert resp.status_code == 200
                body = resp.json()
                assert body["path"] == "aws/cloudtrail"
                assert body["resource_type"] == "core"
                assert body["current"] == "1"
                assert body["versions"] == ["1"]
                assert body["selected"] == "1"
                selected = body["version"]
                assert selected["date"] == "2026-01-01"
                cols = selected["columns"]
                assert cols["total"] == 3

                expr_filter = tc.get(
                    f"{columns_url}?version=1&expr=%40source",
                    headers=headers,
                )
                assert expr_filter.status_code == 200
                assert expr_filter.json()["version"]["columns"]["total"] == 1

                search_name_only = tc.get(
                    f"{columns_url}?version=1&search=z&searchable_columns=name",
                    headers=headers,
                )
                assert search_name_only.status_code == 200
                item = search_name_only.json()["version"]["columns"]["items"][0]
                assert item["name"] == "z_col"
                assert item["_matched_searchable"] == ["name"]

                page2 = tc.get(
                    f"{columns_url}?version=1&page=2&per_page=1",
                    headers=headers,
                )
                assert page2.status_code == 200
                assert page2.json()["version"]["columns"]["items"][0]["name"] == "z_col"

                all_page = tc.get(
                    f"{columns_url}?version=1&per_page=-1",
                    headers=headers,
                )
                assert all_page.status_code == 200
                all_cols = all_page.json()["version"]["columns"]
                assert all_cols["per_page"] == -1
                assert len(all_cols["items"]) == all_cols["total"] == 3
                assert all_cols["items"][0]["_field_type"] == "base"
                assert all_cols["next_page"] is None

                bad_version = tc.get(
                    f"{columns_url}?version=99",
                    headers=headers,
                )
                assert bad_version.status_code == 404
                assert bad_version.json()["code"] == "not_found"

                missing = tc.get(
                    "/api/v1/schemas/definitions/aws/missing/versions/columns?version=1",
                    headers=headers,
                )
                assert missing.status_code == 404
                assert missing.json()["code"] == "not_found"
        finally:
            _registries.clear()

    def test_get_meta_schema_requires_auth(self, client):
        resp = client.get("/api/v1/schemas/definitions/aws/cloudtrail/versions/columns?version=1")
        assert resp.status_code == 401

    def test_get_meta_missing_schema_returns_404(self, client, admin_headers):
        resp = client.get(
            "/api/v1/schemas/definitions/aws/cloudtrail/versions/columns?version=1",
            headers=admin_headers,
        )
        assert resp.status_code == 404
        assert resp.json()["code"] == "not_found"


class TestSchemasMetaWriteRouter:
    """POST/DELETE /api/v1/schemas/definitions/... — meta-schema registry writes."""

    @staticmethod
    def _minimal_schema_body():
        return {
            "current": "1",
            "versions": {
                "1": {
                    "date": "2026-01-01",
                    "type": "model",
                    "summary": "init",
                    "columns": [
                        {
                            "name": "e",
                            "type": "string",
                            "expr": "@source: E",
                            "_field_type": "base",
                        }
                    ],
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
            LocalAuthSettings,
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
            auth=AuthSettings(
                enabled=True,
                auth_dir=str(auth_dir),
                local=LocalAuthSettings(admin_password=ADMIN_PASSWORD),
            ),
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
                invalid_cols = tc.post(
                    "/api/v1/schemas/definitions/gcp/bad_columns",
                    json={
                        **self._minimal_schema_body(),
                        "versions": {
                            "1": {
                                "date": "2026-01-01",
                                "type": "model",
                                "summary": "init",
                                "columns": [
                                    {
                                        "name": "bad",
                                        "type": "not_a_type",
                                        "expr": "@source: X",
                                        "_field_type": "base",
                                    }
                                ],
                            }
                        },
                    },
                    headers=headers,
                )
                assert invalid_cols.status_code == 422
                assert invalid_cols.json()["code"] == "validation_error"
                assert "Column validation failed" in invalid_cols.json()["message"]

                missing_field_type = tc.post(
                    "/api/v1/schemas/definitions/gcp/missing_field_type",
                    json=self._minimal_schema_body()
                    | {
                        "versions": {
                            "1": {
                                "date": "2026-01-01",
                                "type": "model",
                                "summary": "init",
                                "columns": [{"name": "e", "type": "string"}],
                            }
                        }
                    },
                    headers=headers,
                )
                assert missing_field_type.status_code == 422

                core_type = tc.post(
                    "/api/v1/schemas/definitions/gcp/core_type",
                    json={**self._minimal_schema_body(), "resource_type": "core"},
                    headers=headers,
                )
                assert core_type.status_code == 422

                post = tc.post(url, json=self._minimal_schema_body(), headers=headers)
                assert post.status_code == 201
                assert post.json()["path"] == "gcp/audit_log"
                assert post.json()["resource_type"] == "custom"
                assert (schemas_root / "gcp" / "audit_log.yaml").is_file()

                duplicate = tc.post(
                    url,
                    json=self._minimal_schema_body(),
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
                columns_url = f"{url}/versions/columns?version=1"
                gone = tc.get(columns_url, headers=headers)
                assert gone.status_code == 404

                missing_del = tc.delete(url, headers=headers)
                assert missing_del.status_code == 404
        finally:
            _registries.clear()

    def test_create_canonicalizes_schema_path(self, tmp_path):
        from dfe_engine.api.app import create_app
        from dfe_engine.api.deps import _registries, create_access_token
        from dfe_engine.settings import (
            APISettings,
            AuthSettings,
            DFESettings,
            LocalAuthSettings,
            SchemasSettings,
            ServicesSettings,
            SourceSettings,
        )

        schemas_root = tmp_path / "schemas"
        schemas_root.mkdir()
        for name in ("sources", "services", "auth"):
            (tmp_path / name).mkdir()

        settings = DFESettings(
            config_dir=str(tmp_path),
            schemas=SchemasSettings(schemas_dir=str(schemas_root)),
            source=SourceSettings(sources_dir=str(tmp_path / "sources")),
            services=ServicesSettings(config_yaml_dir=str(tmp_path / "services")),
            auth=AuthSettings(
                enabled=True,
                auth_dir=str(tmp_path / "auth"),
                local=LocalAuthSettings(admin_password=ADMIN_PASSWORD),
            ),
            api=APISettings(jwt_secret="test-secret-key-for-unit-tests-phase4-schemas-write"),
        )
        app = create_app(settings)
        token = create_access_token(
            data={"sub": "admin", "org_id": "test-org", "roles": ["admin"]},
            settings=settings,
        )
        headers = {"Authorization": f"Bearer {token}"}

        try:
            with TestClient(app, raise_server_exceptions=False) as tc:
                messy_url = "/api/v1/schemas/definitions/gcp//audit_log"
                post = tc.post(
                    messy_url,
                    json=self._minimal_schema_body(),
                    headers=headers,
                )
                assert post.status_code == 201
                assert post.json()["path"] == "gcp/audit_log"
                assert (schemas_root / "gcp" / "audit_log.yaml").is_file()

                get_resp = tc.get(
                    "/api/v1/schemas/definitions/gcp/audit_log/versions/columns?version=1",
                    headers=headers,
                )
                assert get_resp.status_code == 200

                messy_get = tc.get(
                    f"{messy_url}/versions/columns?version=1",
                    headers=headers,
                )
                assert messy_get.status_code == 200
                assert messy_get.json()["path"] == "gcp/audit_log"

                body_with_slashes = {
                    **self._minimal_schema_body(),
                    "path": "azure\\activity",
                }
                create_azure = tc.post(
                    "/api/v1/schemas/definitions/azure/activity",
                    json=body_with_slashes,
                    headers=headers,
                )
                assert create_azure.status_code == 201
                assert create_azure.json()["path"] == "azure/activity"
        finally:
            _registries.clear()

    def test_create_forbidden_for_readonly_role(self, tmp_path):
        from dfe_engine.api.app import create_app
        from dfe_engine.api.deps import _registries, create_access_token
        from dfe_engine.settings import (
            APISettings,
            AuthSettings,
            DFESettings,
            LocalAuthSettings,
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
            auth=AuthSettings(
                enabled=True,
                auth_dir=str(tmp_path / "auth"),
                local=LocalAuthSettings(admin_password=ADMIN_PASSWORD),
            ),
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

    def test_patch_meta_schema_current_and_summary(self, tmp_path):
        from dfe_engine.api.app import create_app
        from dfe_engine.api.deps import _registries, create_access_token
        from dfe_engine.settings import (
            APISettings,
            AuthSettings,
            DFESettings,
            LocalAuthSettings,
            SchemasSettings,
            ServicesSettings,
            SourceSettings,
        )
        from dfe_engine.yaml_utils import yaml_dump

        schemas_root = tmp_path / "schemas"
        (schemas_root / "aws").mkdir(parents=True)
        yaml_dump(
            {
                "current": "1.0.0",
                "versions": {
                    "1.0.0": {
                        "date": "2026-01-01",
                        "type": "model",
                        "summary": "init",
                        "columns": [{"name": "e", "type": "string"}],
                    },
                    "1.1.0": {
                        "date": "2026-02-01",
                        "type": "addition",
                        "summary": "extra",
                        "columns": [
                            {"name": "e", "type": "string"},
                            {"name": "n", "type": "integer"},
                        ],
                    },
                },
            },
            schemas_root / "aws" / "cloudtrail.yaml",
        )

        for name in ("sources", "services", "auth"):
            (tmp_path / name).mkdir()

        settings = DFESettings(
            config_dir=str(tmp_path),
            schemas=SchemasSettings(schemas_dir=str(schemas_root)),
            source=SourceSettings(sources_dir=str(tmp_path / "sources")),
            services=ServicesSettings(config_yaml_dir=str(tmp_path / "services")),
            auth=AuthSettings(
                enabled=True,
                auth_dir=str(tmp_path / "auth"),
                local=LocalAuthSettings(admin_password=ADMIN_PASSWORD),
            ),
            api=APISettings(jwt_secret="test-secret-key-for-unit-tests-phase4-schemas-patch"),
        )
        app = create_app(settings)
        token = create_access_token(
            data={"sub": "admin", "org_id": "test-org", "roles": ["admin"]},
            settings=settings,
        )
        headers = {"Authorization": f"Bearer {token}"}
        base = "/api/v1/schemas/definitions/aws/cloudtrail"

        try:
            with TestClient(app, raise_server_exceptions=False) as tc:
                set_current = tc.patch(base, json={"current": "1.1.0"}, headers=headers)
                assert set_current.status_code == 200
                set_body = set_current.json()
                assert set_body["current"] == "1.1.0"
                assert set_body["path"] == "aws/cloudtrail"
                assert "1.1.0" in set_body["versions"]

                summary = tc.patch(
                    f"{base}?version=1.0.0",
                    json={"summary": "updated init"},
                    headers=headers,
                )
                assert summary.status_code == 200
                summary_body = summary.json()
                assert summary_body["current"] == "1.1.0"
                get_def = tc.get(
                    f"{base}/versions/columns?version=1.0.0",
                    headers=headers,
                )
                assert get_def.status_code == 200
                assert get_def.json()["version"]["summary"] == "updated init"

                add = tc.post(
                    f"{base}/versions",
                    json={
                        "type": "revision",
                        "summary": "added column p",
                        "columns": [
                            {"name": "e", "type": "string", "_field_type": "base"},
                            {"name": "n", "type": "integer", "_field_type": "base"},
                            {"name": "p", "type": "boolean", "_field_type": "base"},
                        ],
                    },
                    headers=headers,
                )
                assert add.status_code == 201
                body = add.json()
                assert body["path"] == "aws/cloudtrail"
                assert body["current"] == "1.1.1"
                assert "1.1.1" in body["versions"]
                assert "1.0.0" in body["versions"]
                get_new = tc.get(
                    f"{base}/versions/columns?version=1.1.1",
                    headers=headers,
                )
                assert get_new.status_code == 200
                ver = get_new.json()["version"]
                assert ver["type"] == "revision"
                assert ver["summary"] == "added column p"
                assert len(ver["columns"]["items"]) == 3

                empty_cols = tc.post(
                    f"{base}/versions",
                    json={"type": "model", "columns": []},
                    headers=headers,
                )
                assert empty_cols.status_code == 422
                assert "validation_error" in empty_cols.text
                assert "column" in empty_cols.text.lower()
        finally:
            _registries.clear()

    def test_delete_meta_schema_version(self, tmp_path):
        from dfe_engine.api.app import create_app
        from dfe_engine.api.deps import _registries, create_access_token
        from dfe_engine.settings import (
            APISettings,
            AuthSettings,
            DFESettings,
            LocalAuthSettings,
            SchemasSettings,
            ServicesSettings,
            SourceSettings,
        )
        from dfe_engine.yaml_utils import yaml_dump

        schemas_root = tmp_path / "schemas"
        (schemas_root / "aws").mkdir(parents=True)
        yaml_dump(
            {
                "current": "1.1.0",
                "versions": {
                    "1.0.0": {
                        "date": "2026-01-01",
                        "type": "model",
                        "summary": "init",
                        "columns": [{"name": "e", "type": "string"}],
                    },
                    "1.1.0": {
                        "date": "2026-02-01",
                        "type": "addition",
                        "summary": "extra",
                        "columns": [
                            {"name": "e", "type": "string"},
                            {"name": "n", "type": "integer"},
                        ],
                    },
                },
            },
            schemas_root / "aws" / "cloudtrail.yaml",
        )
        (schemas_root / "core").mkdir()
        yaml_dump(
            {
                "resource_type": "core",
                "current": "1.1.0",
                "versions": {
                    "1.0.0": {
                        "date": "2026-01-01",
                        "type": "model",
                        "summary": "init",
                        "columns": [{"name": "a", "type": "string"}],
                    },
                    "1.1.0": {
                        "date": "2026-02-01",
                        "type": "addition",
                        "summary": "extra",
                        "columns": [{"name": "a", "type": "string"}],
                    },
                },
            },
            schemas_root / "core" / "protected.yaml",
        )

        for name in ("sources", "services", "auth"):
            (tmp_path / name).mkdir()

        settings = DFESettings(
            config_dir=str(tmp_path),
            schemas=SchemasSettings(schemas_dir=str(schemas_root)),
            source=SourceSettings(sources_dir=str(tmp_path / "sources")),
            services=ServicesSettings(config_yaml_dir=str(tmp_path / "services")),
            auth=AuthSettings(
                enabled=True,
                auth_dir=str(tmp_path / "auth"),
                local=LocalAuthSettings(admin_password=ADMIN_PASSWORD),
            ),
            api=APISettings(jwt_secret="test-secret-key-for-unit-tests-phase4-schemas-delver"),
        )
        app = create_app(settings)
        token = create_access_token(
            data={"sub": "admin", "org_id": "test-org", "roles": ["admin"]},
            settings=settings,
        )
        headers = {"Authorization": f"Bearer {token}"}
        base = "/api/v1/schemas/definitions/aws/cloudtrail"

        try:
            with TestClient(app, raise_server_exceptions=False) as tc:
                deleted = tc.delete(f"{base}/versions/1.0.0", headers=headers)
                assert deleted.status_code == 204
                gone = tc.get(f"{base}/versions/columns?version=1.0.0", headers=headers)
                assert gone.status_code == 404
                remaining = tc.get(f"{base}/versions/columns?version=1.1.0", headers=headers)
                assert remaining.status_code == 200

                last = tc.delete(f"{base}/versions/1.1.0", headers=headers)
                assert last.status_code == 422
                assert last.json()["code"] == "validation_error"

                missing_ver = tc.delete(f"{base}/versions/9.9.9", headers=headers)
                assert missing_ver.status_code == 404
                missing_schema = tc.delete(
                    "/api/v1/schemas/definitions/aws/missing/versions/1.0.0",
                    headers=headers,
                )
                assert missing_schema.status_code == 404

                core_del = tc.delete(
                    "/api/v1/schemas/definitions/core/protected/versions/1.0.0",
                    headers=headers,
                )
                assert core_del.status_code == 409
                assert core_del.json()["code"] == "conflict"
                assert core_del.json()["message"] == "Core resources can't be mutated"
        finally:
            _registries.clear()

    def test_delete_meta_schema_version_requires_auth(self, client):
        resp = client.delete("/api/v1/schemas/definitions/aws/cloudtrail/versions/1.0.0")
        assert resp.status_code == 401
