"""Tests for the services router."""

import json

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
        config_dir=str(tmp_path),
        source=SourceSettings(sources_dir=str(tmp_path / "sources")),
        services=ServicesSettings(config_yaml_dir=str(services_dir)),
        auth=AuthSettings(
            enabled=True,
            auth_dir=str(tmp_path / "auth"),
        ),
        api=APISettings(jwt_secret="test-secret-hmac-key-at-least-32-bytes"),
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


class TestSecretPlaceholderIsNotSaved:
    """A read shows each set secret as pydantic's placeholder; writing it back is refused."""

    URL = "/api/v1/services/receiver/placeholder"

    def _body(self) -> dict:
        return {
            "kafka": {
                "brokers": ["k:9092"],
                "sasl": {"enabled": True, "username": "dfe", "password": "sasl-pw-8810"},
            }
        }

    def _stored_retries(self) -> int:
        from dfe_engine.api.deps import _registries

        config = _registries["service_config"].get_config("receiver", "placeholder")
        return config.kafka.producer.retries

    def test_a_read_written_back_is_refused_and_nothing_saved(self, client, admin_headers):
        assert client.put(self.URL, json=self._body(), headers=admin_headers).status_code == 200
        shown = client.get(self.URL, headers=admin_headers).json()["config"]
        assert shown["kafka"]["sasl"]["password"] == "**********"
        assert "sasl-pw-8810" not in json.dumps(shown)
        retries = self._stored_retries()

        shown["kafka"]["producer"]["retries"] = retries + 7
        resp = client.put(self.URL, json=shown, headers=admin_headers)
        assert resp.status_code == 400, resp.text
        assert resp.json()["code"] == "masked_value"
        assert "kafka.sasl.password" in resp.json()["message"]
        assert self._stored_retries() == retries

    def test_the_placeholder_is_refused_wherever_it_sits(self, client, admin_headers):
        body = {"server": {"auth": {"bearer": {"tokens": ["tok-8811", "**********"]}}}}
        resp = client.put(self.URL, json=body, headers=admin_headers)
        assert resp.status_code == 400, resp.text
        assert "server.auth.bearer.tokens[1]" in resp.json()["message"]

    def test_a_real_secret_is_accepted(self, client, admin_headers):
        assert client.put(self.URL, json=self._body(), headers=admin_headers).status_code == 200


class TestServicesSeed:
    def test_seed(self, client, admin_headers):
        resp = client.post("/api/v1/services/seed", headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "seeded" in data

    def test_seed_requires_write(self, client, viewer_headers):
        resp = client.post("/api/v1/services/seed", headers=viewer_headers)
        assert resp.status_code == 403
