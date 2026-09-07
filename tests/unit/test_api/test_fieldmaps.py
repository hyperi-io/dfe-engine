"""Tests for the field maps router."""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app_with_fieldmaps(tmp_path):
    """App fixture with FieldMapRegistry initialized."""
    from dfe_engine.api.app import create_app
    from dfe_engine.api.deps import _registries
    from dfe_engine.settings import (
        APISettings,
        AuthSettings,
        DFESettings,
        FieldMapSettings,
        LocalAuthSettings,
        ServicesSettings,
        SourceSettings,
    )

    fm_dir = tmp_path / "fieldmaps"
    fm_dir.mkdir()

    settings = DFESettings(
        config_dir=str(tmp_path),
        source=SourceSettings(sources_dir=str(tmp_path / "sources")),
        services=ServicesSettings(config_yaml_dir=str(tmp_path / "services")),
        fieldmap=FieldMapSettings(fieldmaps_dir=str(fm_dir)),
        auth=AuthSettings(
            enabled=True,
            auth_dir=str(tmp_path / "auth"),
            # Production posture refuses to start on the shipped admin password.
            local=LocalAuthSettings(admin_password="test-admin-pw"),
        ),
        api=APISettings(jwt_secret="test-secret-hmac-key-at-least-32-bytes"),
    )
    for d in ["sources", "services"]:
        (tmp_path / d).mkdir(exist_ok=True)

    app = create_app(settings=settings)
    yield app
    _registries.clear()


@pytest.fixture
def fm_client(app_with_fieldmaps):
    with TestClient(app_with_fieldmaps, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def fm_admin_headers(app_with_fieldmaps):
    from dfe_engine.api.deps import create_access_token
    from dfe_engine.settings import APISettings, DFESettings

    settings = DFESettings(api=APISettings(jwt_secret="test-secret-hmac-key-at-least-32-bytes"))
    token = create_access_token(
        data={"sub": "admin", "org_id": "test-org", "roles": ["admin"]},
        settings=settings,
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def sample_fieldmap():
    return {
        "standard": "sigma",
        "source": None,
        "version": "1.0.0",
        "mappings": {
            "Image": "_json.process.executable",
            "EventID": "_json.event.code",
        },
    }


class TestFieldMapsList:
    def test_list_empty(self, fm_client, fm_admin_headers):
        resp = fm_client.get("/api/v1/field-maps", headers=fm_admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["items"] == []

    def test_list_requires_auth(self, fm_client):
        resp = fm_client.get("/api/v1/field-maps")
        assert resp.status_code == 401


class TestFieldMapsNotFound:
    def test_get_missing_standard(self, fm_client, fm_admin_headers):
        resp = fm_client.get("/api/v1/field-maps/ecs", headers=fm_admin_headers)
        assert resp.status_code == 404
        assert resp.json()["code"] == "not_found"

    def test_get_missing_source(self, fm_client, fm_admin_headers):
        resp = fm_client.get("/api/v1/field-maps/sigma/myhost", headers=fm_admin_headers)
        assert resp.status_code == 404

    def test_delete_missing(self, fm_client, fm_admin_headers):
        resp = fm_client.delete("/api/v1/field-maps/sigma/myhost", headers=fm_admin_headers)
        assert resp.status_code == 404


class TestFieldMapsSeed:
    def test_seed(self, fm_client, fm_admin_headers):
        resp = fm_client.post("/api/v1/field-maps/seed", headers=fm_admin_headers)
        assert resp.status_code == 200
        assert "seeded" in resp.json()
