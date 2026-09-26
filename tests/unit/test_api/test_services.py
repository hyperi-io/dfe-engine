"""Tests for the services router."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dfe_engine.appmgmt import contract
from dfe_engine.settings import DFESettings, ServicesSettings
from dfe_engine.yaml_utils import yaml_load_string


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


MASK = "**********"
SECRETS = ("sasl-pw-8810", "hdr-8812", "tok-8813")


def _saved(api_settings: DFESettings, table: str) -> dict:
    """The YAML file a service reads, as the registry wrote it."""
    path = Path(api_settings.services.config_yaml_dir) / f"{table}.yaml"
    return yaml_load_string(path.read_text(encoding="utf-8"))


class TestLegacySecretsAreStoredAndMaskedOnTheWayOut:
    """The file the app reads holds each secret in full, and a read shows it masked."""

    URL = "/api/v1/services/receiver/placeholder"

    def _body(self) -> dict:
        return {
            "kafka": {
                "brokers": ["k:9092"],
                "sasl": {"enabled": True, "username": "dfe", "password": "sasl-pw-8810"},
            },
            "server": {
                "auth": {
                    "mode": "both",
                    "accepted_headers": [{"name": "x-key", "values": ["hdr-8812"]}],
                    "bearer": {"tokens": ["tok-8813"]},
                }
            },
        }

    def test_a_secret_is_saved_in_full_and_read_back_masked(
        self, client, admin_headers, api_settings
    ):
        assert client.put(self.URL, json=self._body(), headers=admin_headers).status_code == 200
        saved = _saved(api_settings, "receiver-placeholder")
        assert saved["kafka"]["sasl"]["password"] == "sasl-pw-8810"
        assert saved["server"]["auth"]["accepted_headers"] == [
            {"name": "x-key", "values": ["hdr-8812"]}
        ]
        assert saved["server"]["auth"]["bearer"]["tokens"] == ["tok-8813"]

        resp = client.get(self.URL, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        shown = resp.json()["config"]
        assert shown["kafka"]["sasl"]["password"] == MASK
        assert shown["server"]["auth"]["accepted_headers"] == [{"name": "x-key", "values": [MASK]}]
        assert shown["server"]["auth"]["bearer"]["tokens"] == [MASK]
        for secret in SECRETS:
            assert secret not in resp.text

    def test_a_read_written_back_keeps_every_stored_secret(
        self, client, admin_headers, api_settings
    ):
        assert client.put(self.URL, json=self._body(), headers=admin_headers).status_code == 200
        shown = client.get(self.URL, headers=admin_headers).json()["config"]
        retries = shown["kafka"]["producer"]["retries"]
        shown["kafka"]["producer"]["retries"] = retries + 7

        resp = client.put(self.URL, json=shown, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        saved = _saved(api_settings, "receiver-placeholder")
        assert saved["kafka"]["producer"]["retries"] == retries + 7
        assert saved["kafka"]["sasl"]["password"] == "sasl-pw-8810"
        assert saved["server"]["auth"]["accepted_headers"][0]["values"] == ["hdr-8812"]
        assert saved["server"]["auth"]["bearer"]["tokens"] == ["tok-8813"]
        assert MASK not in json.dumps(saved)

    def test_the_placeholder_with_nothing_stored_is_refused(
        self, client, admin_headers, api_settings
    ):
        body = {"server": {"auth": {"bearer": {"tokens": ["tok-8811", MASK]}}}}
        resp = client.put(self.URL, json=body, headers=admin_headers)
        assert resp.status_code == 400, resp.text
        assert resp.json()["code"] == "masked_value"
        assert "server.auth.bearer.tokens[1]" in resp.json()["message"]
        path = Path(api_settings.services.config_yaml_dir) / "receiver-placeholder.yaml"
        assert not path.exists()

    def test_a_schema_less_config_is_masked_by_name(self, client, admin_headers, api_settings):
        url = "/api/v1/services/customsvc/one"
        body = {"db": {"host": "db-1", "password": "raw-pw-8814"}}
        assert client.put(url, json=body, headers=admin_headers).status_code == 200
        resp = client.get(url, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["config"]["db"] == {"host": "db-1", "password": contract.REDACTED}
        assert "raw-pw-8814" not in resp.text
        assert _saved(api_settings, "customsvc-one")["db"]["password"] == "raw-pw-8814"


@pytest.fixture
def stored_before_retirement(api_settings: DFESettings) -> None:
    """Service files as a deployment saved them before ``payload`` and ``format`` were retired."""
    services = Path(api_settings.services.config_yaml_dir)
    (services / "loader-old.yaml").write_text(
        "clickhouse:\n  password: ch-pw-8815\npayload:\n  format: auto\n  mismatch_threshold: 10\n",
        encoding="utf-8",
    )
    (services / "transform-vrl-old.yaml").write_text(
        "source:\n  brokers: [k:9092]\n  format: auto\n", encoding="utf-8"
    )


class TestAConfigStoredBeforeAKeyWasRetired:
    """An upgrade keeps its stored files, so a key since retired must not fail the read."""

    @pytest.mark.parametrize(
        "url", ["/api/v1/services/loader/old", "/api/v1/services/transform-vrl/old"]
    )
    def test_the_read_answers(self, stored_before_retirement, client, admin_headers, url):
        resp = client.get(url, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        assert "payload" not in resp.json()["config"]
        assert "format" not in resp.json()["config"].get("source", {})
        assert "ch-pw-8815" not in resp.text


class TestServicesSeed:
    def test_seed(self, client, admin_headers):
        resp = client.post("/api/v1/services/seed", headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "seeded" in data

    def test_seed_requires_write(self, client, viewer_headers):
        resp = client.post("/api/v1/services/seed", headers=viewer_headers)
        assert resp.status_code == 403
