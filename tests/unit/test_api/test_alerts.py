"""Tests for the alerts router — alert destination CRUD."""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app_with_alerts(tmp_path):
    """App fixture with alert destinations store initialized."""
    from dfe_engine.api.app import create_app
    from dfe_engine.api.deps import _registries
    from dfe_engine.settings import (
        APISettings,
        AuthSettings,
        DFESettings,
        HuntsSettings,
        LocalAuthSettings,
        ServicesSettings,
        SourceSettings,
    )

    alert_dir = tmp_path / "alert-destinations"
    alert_dir.mkdir()

    settings = DFESettings(
        source=SourceSettings(sources_dir=str(tmp_path / "sources")),
        services=ServicesSettings(config_yaml_dir=str(tmp_path / "services")),
        hunts=HuntsSettings(alert_destinations_dir=str(alert_dir)),
        auth=AuthSettings(
            enabled=True,
            local=LocalAuthSettings(admin_password="admin-pw", org_id="test-org"),
        ),
        api=APISettings(jwt_secret="test-secret"),
    )
    for d in ["sources", "services"]:
        (tmp_path / d).mkdir(exist_ok=True)

    app = create_app(settings=settings)
    yield app
    _registries.clear()


@pytest.fixture
def alert_client(app_with_alerts):
    with TestClient(app_with_alerts, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def alert_admin_headers(app_with_alerts):
    from dfe_engine.api.deps import create_access_token
    from dfe_engine.settings import APISettings, DFESettings

    settings = DFESettings(api=APISettings(jwt_secret="test-secret"))
    token = create_access_token(
        data={"sub": "admin", "org_id": "test-org", "roles": ["admin"]},
        settings=settings,
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def sample_destination():
    return {
        "name": "slack-alerts",
        "url": "slack://T00000000/B00000000/X0000000000000000000000/",
        "description": "Slack channel for DFE alerts",
        "enabled": True,
    }


class TestAlertDestinationsList:
    def test_list_empty(self, alert_client, alert_admin_headers):
        resp = alert_client.get("/api/v1/alerts/destinations", headers=alert_admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["items"] == []
        assert data["total"] == 0

    def test_list_requires_auth(self, alert_client):
        resp = alert_client.get("/api/v1/alerts/destinations")
        assert resp.status_code == 401


class TestAlertDestinationsCRUD:
    def test_create_destination(self, alert_client, alert_admin_headers, sample_destination):
        resp = alert_client.post(
            "/api/v1/alerts/destinations",
            json=sample_destination,
            headers=alert_admin_headers,
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "slack-alerts"
        assert data["url"] == sample_destination["url"]
        assert data["enabled"] is True

    def test_create_then_list(self, alert_client, alert_admin_headers, sample_destination):
        alert_client.post(
            "/api/v1/alerts/destinations",
            json=sample_destination,
            headers=alert_admin_headers,
        )
        resp = alert_client.get("/api/v1/alerts/destinations", headers=alert_admin_headers)
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["name"] == "slack-alerts"
        assert data["items"][0]["url_scheme"] == "slack"

    def test_get_destination(self, alert_client, alert_admin_headers, sample_destination):
        alert_client.post(
            "/api/v1/alerts/destinations",
            json=sample_destination,
            headers=alert_admin_headers,
        )
        resp = alert_client.get(
            "/api/v1/alerts/destinations/slack-alerts",
            headers=alert_admin_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "slack-alerts"

    def test_get_not_found(self, alert_client, alert_admin_headers):
        resp = alert_client.get(
            "/api/v1/alerts/destinations/nonexistent",
            headers=alert_admin_headers,
        )
        assert resp.status_code == 404
        assert resp.json()["code"] == "not_found"

    def test_update_destination(self, alert_client, alert_admin_headers, sample_destination):
        alert_client.post(
            "/api/v1/alerts/destinations",
            json=sample_destination,
            headers=alert_admin_headers,
        )
        updated = {**sample_destination, "description": "Updated description", "enabled": False}
        resp = alert_client.put(
            "/api/v1/alerts/destinations/slack-alerts",
            json=updated,
            headers=alert_admin_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["description"] == "Updated description"
        assert resp.json()["enabled"] is False

    def test_delete_destination(self, alert_client, alert_admin_headers, sample_destination):
        alert_client.post(
            "/api/v1/alerts/destinations",
            json=sample_destination,
            headers=alert_admin_headers,
        )
        resp = alert_client.delete(
            "/api/v1/alerts/destinations/slack-alerts",
            headers=alert_admin_headers,
        )
        assert resp.status_code == 204

        # Verify gone
        resp = alert_client.get(
            "/api/v1/alerts/destinations/slack-alerts",
            headers=alert_admin_headers,
        )
        assert resp.status_code == 404

    def test_duplicate_create(self, alert_client, alert_admin_headers, sample_destination):
        alert_client.post(
            "/api/v1/alerts/destinations",
            json=sample_destination,
            headers=alert_admin_headers,
        )
        resp = alert_client.post(
            "/api/v1/alerts/destinations",
            json=sample_destination,
            headers=alert_admin_headers,
        )
        assert resp.status_code == 409
        assert resp.json()["code"] == "conflict"

    def test_viewer_read_allowed(self, alert_client, alert_admin_headers):
        from dfe_engine.api.deps import create_access_token
        from dfe_engine.settings import APISettings, DFESettings

        settings = DFESettings(api=APISettings(jwt_secret="test-secret"))
        token = create_access_token(
            data={"sub": "viewer", "org_id": "test-org", "roles": ["infra_viewer"]},
            settings=settings,
        )
        viewer_headers = {"Authorization": f"Bearer {token}"}

        resp = alert_client.get("/api/v1/alerts/destinations", headers=viewer_headers)
        assert resp.status_code == 200

    def test_viewer_write_forbidden(self, alert_client, sample_destination):
        from dfe_engine.api.deps import create_access_token
        from dfe_engine.settings import APISettings, DFESettings

        settings = DFESettings(api=APISettings(jwt_secret="test-secret"))
        token = create_access_token(
            data={"sub": "viewer", "org_id": "test-org", "roles": ["infra_viewer"]},
            settings=settings,
        )
        viewer_headers = {"Authorization": f"Bearer {token}"}

        resp = alert_client.post(
            "/api/v1/alerts/destinations",
            json=sample_destination,
            headers=viewer_headers,
        )
        assert resp.status_code == 403
