"""Tests for the DFE Engine API application — create_app, lifespan, health, OpenAPI."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from dfe_engine.api.app import _get_version, create_app
from dfe_engine.api.deps import _registries
from dfe_engine.settings import (
    APISettings,
    AuthSettings,
    DFESettings,
    LocalAuthSettings,
    ServicesSettings,
    SourceSettings,
)


@pytest.fixture
def app_settings(tmp_path):
    """Minimal settings for app tests."""
    (tmp_path / "sources").mkdir(exist_ok=True)
    (tmp_path / "services").mkdir(exist_ok=True)
    return DFESettings(
        source=SourceSettings(sources_dir=str(tmp_path / "sources")),
        services=ServicesSettings(config_yaml_dir=str(tmp_path / "services")),
        auth=AuthSettings(
            enabled=True,
            local=LocalAuthSettings(admin_password="pw", org_id="org"),
        ),
        api=APISettings(jwt_secret="secret"),
    )


@pytest.fixture
def app_client(app_settings):
    """TestClient for app tests."""
    app = create_app(settings=app_settings)
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    _registries.clear()


class TestCreateApp:
    def test_create_app_with_settings(self, app_settings):
        app = create_app(settings=app_settings)
        _registries.clear()
        assert app.title == "DFE Engine API"
        assert app.state.settings is app_settings

    def test_create_app_with_cors_override(self, app_settings, tmp_path):
        app = create_app(
            settings=app_settings,
            cors_origins=["https://custom.example.com"],
        )
        _registries.clear()
        # CORS middleware is added; we verify app builds without error
        assert app.title == "DFE Engine API"


class TestHealth:
    def test_health_returns_200_and_status(self, app_client: TestClient):
        resp = app_client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "healthy"
        assert "version" in data

    def test_health_no_auth_required(self, app_client: TestClient):
        """Health endpoint is unauthenticated."""
        resp = app_client.get("/health", headers={})
        assert resp.status_code == 200


class TestOpenAPI:
    def test_openapi_schema_includes_bearer_auth(self, app_client: TestClient):
        resp = app_client.get("/openapi.json")
        assert resp.status_code == 200
        schema = resp.json()
        security_schemes = schema.get("components", {}).get("securitySchemes", {})
        assert "BearerAuth" in security_schemes
        assert security_schemes["BearerAuth"]["type"] == "http"
        assert security_schemes["BearerAuth"]["scheme"] == "bearer"

    def test_api_paths_have_security_requirement(self, app_client: TestClient):
        resp = app_client.get("/openapi.json")
        assert resp.status_code == 200
        schema = resp.json()
        paths = schema.get("paths", {})
        api_paths = [p for p in paths if p.startswith("/api/")]
        assert len(api_paths) > 0
        for path_key in api_paths:
            path_item = paths[path_key]
            for _, spec in path_item.items():
                if isinstance(spec, dict) and "security" in spec:
                    assert any("BearerAuth" in s for s in spec["security"])

    def test_openapi_schema_cached_on_second_request(self, app_client: TestClient):
        """Second /openapi.json returns cached schema (covers custom_openapi early return)."""
        resp1 = app_client.get("/openapi.json")
        resp2 = app_client.get("/openapi.json")
        assert resp1.status_code == 200 and resp2.status_code == 200
        assert resp1.json() == resp2.json()


class TestDocs:
    def test_docs_reachable(self, app_client: TestClient):
        resp = app_client.get("/docs")
        assert resp.status_code == 200

    def test_redoc_reachable(self, app_client: TestClient):
        resp = app_client.get("/redoc")
        assert resp.status_code == 200


class TestGetVersion:
    def test_get_version_returns_package_version(self):
        v = _get_version()
        assert isinstance(v, str)
        assert len(v) > 0

    def test_get_version_fallback_to_dev_on_import_error(self):
        with patch(
            "importlib.metadata.version",
            side_effect=Exception("package not found"),
        ):
            v = _get_version()
            assert v == "dev"
