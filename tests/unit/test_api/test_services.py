"""Tests for the services router."""

import pytest
from fastapi.testclient import TestClient

from dfe_engine.settings import DFESettings, ServicesSettings


@pytest.fixture
def app_with_services(tmp_path):
    """App fixture with ServiceConfigRegistry initialized."""
    from dfe_engine.api.app import create_app
    from dfe_engine.api.deps import _registries
    from dfe_engine.settings import APISettings, AuthSettings, SourceSettings

    services_dir = tmp_path / "services"
    services_dir.mkdir()

    settings = DFESettings(
        source=SourceSettings(sources_dir=str(tmp_path / "sources")),
        services=ServicesSettings(config_yaml_dir=str(services_dir)),
        auth=AuthSettings(
            enabled=True,
            auth_dir=str(tmp_path / "auth"),
        ),
        api=APISettings(jwt_secret="test-secret"),
    )
    (tmp_path / "sources").mkdir(exist_ok=True)
    app = create_app(settings=settings)
    yield app
    _registries.clear()


@pytest.fixture
def svc_client(app_with_services, api_settings, admin_token):
    """TestClient for services tests."""
    with TestClient(app_with_services, raise_server_exceptions=False) as c:
        yield c


class TestServicesList:
    def test_list_empty(self, client, admin_headers):
        resp = client.get("/api/v1/services", headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["items"] == []
        assert data["total"] == 0

    def test_list_requires_auth(self, client):
        resp = client.get("/api/v1/services")
        assert resp.status_code == 401

    def test_list_viewer_allowed(self, client, viewer_headers):
        resp = client.get("/api/v1/services", headers=viewer_headers)
        assert resp.status_code == 200


class TestServiceConfigNotFound:
    def test_get_missing(self, client, admin_headers):
        resp = client.get("/api/v1/services/receiver/production", headers=admin_headers)
        assert resp.status_code == 404
        assert resp.json()["code"] == "not_found"

    def test_delete_missing(self, client, admin_headers):
        resp = client.delete("/api/v1/services/receiver/production", headers=admin_headers)
        assert resp.status_code == 404


class TestServicesSeed:
    def test_seed(self, client, admin_headers):
        resp = client.post("/api/v1/services/seed", headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "seeded" in data

    def test_seed_requires_write(self, client, viewer_headers):
        resp = client.post("/api/v1/services/seed", headers=viewer_headers)
        assert resp.status_code == 403
