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

    def test_build_unknown_source_version(self, client, admin_headers, sample_source):
        client.post("/api/v1/sources", json=sample_source, headers=admin_headers)
        resp = client.post(
            f"/api/v1/schemas/{sample_source['source']}/build?version=9.9.9",
            headers=admin_headers,
        )
        assert resp.status_code == 404

    def test_columns_with_existing_source(self, client, admin_headers, sample_source):
        """Create a source, then try to get columns (404 when version has no schema YAML refs)."""
        client.post("/api/v1/sources", json=sample_source, headers=admin_headers)
        resp = client.get(
            f"/api/v1/schemas/{sample_source['source']}/columns",
            headers=admin_headers,
        )
        assert resp.status_code == 404
        assert resp.json()["code"] == "no_schema"

    def test_columns_unknown_source_version(self, client, admin_headers, sample_source):
        client.post("/api/v1/sources", json=sample_source, headers=admin_headers)
        resp = client.get(
            f"/api/v1/schemas/{sample_source['source']}/columns?version=9.9.9",
            headers=admin_headers,
        )
        assert resp.status_code == 404
        assert "9.9.9" in resp.json()["message"]

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
                    "columns": [{"name": "e", "type": "string", "expr": "@source: E"}],
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
                            {"name": "z_col", "type": "integer"},
                            {"name": "a_col", "type": "string"},
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
                columns_url = "/api/v1/schemas/definitions/aws/cloudtrail/versions/columns"
                resp = tc.get(f"{columns_url}?version=1", headers=headers)
                assert resp.status_code == 200
                body = resp.json()
                assert body["path"] == "aws/cloudtrail"
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

    def test_get_meta_not_configured_returns_503(self, client, admin_headers):
        resp = client.get(
            "/api/v1/schemas/definitions/aws/cloudtrail/versions/columns?version=1",
            headers=admin_headers,
        )
        assert resp.status_code == 503
        assert resp.json()["code"] == "not_configured"


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

                post = tc.post(url, json=self._minimal_schema_body(), headers=headers)
                assert post.status_code == 201
                assert post.json()["path"] == "gcp/audit_log"
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
            auth=AuthSettings(enabled=True, auth_dir=str(tmp_path / "auth")),
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

    def test_patch_meta_schema_current_and_summary(self, tmp_path):
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
            auth=AuthSettings(enabled=True, auth_dir=str(tmp_path / "auth")),
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
                assert set_current.json()["current"] == "1.1.0"

                summary = tc.patch(
                    f"{base}?version=1.0.0",
                    json={"summary": "updated init"},
                    headers=headers,
                )
                assert summary.status_code == 200
                assert summary.json()["versions"]["1.0.0"]["summary"] == "updated init"

                add = tc.post(
                    f"{base}/versions",
                    json={
                        "type": "revision",
                        "summary": "added column p",
                        "columns": [
                            {"name": "e", "type": "string"},
                            {"name": "n", "type": "integer"},
                            {"name": "p", "type": "boolean"},
                        ],
                    },
                    headers=headers,
                )
                assert add.status_code == 201
                body = add.json()
                assert body["current"] == "1.1.1"
                assert "1.1.1" in body["versions"]
                assert body["versions"]["1.1.1"]["type"] == "revision"
                assert body["versions"]["1.1.1"]["summary"] == "added column p"
                assert len(body["versions"]["1.1.1"]["columns"]) == 3
                assert "1.0.0" in body["versions"]

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
