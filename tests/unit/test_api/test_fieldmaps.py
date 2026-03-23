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
        source=SourceSettings(sources_dir=str(tmp_path / "sources")),
        services=ServicesSettings(config_yaml_dir=str(tmp_path / "services")),
        fieldmap=FieldMapSettings(fieldmaps_dir=str(fm_dir)),
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
def fm_client(app_with_fieldmaps):
    with TestClient(app_with_fieldmaps, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def fm_admin_headers(app_with_fieldmaps):
    from dfe_engine.api.deps import create_access_token
    from dfe_engine.settings import APISettings, DFESettings

    settings = DFESettings(api=APISettings(jwt_secret="test-secret"))
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

    def test_list_with_group_by_standard(self, fm_client, fm_admin_headers, sample_fieldmap):
        """When group_by=standard, items are group keys and object maps key to list of maps."""
        fm_client.post("/api/v1/field-maps/seed", headers=fm_admin_headers)
        fm_client.post(
            "/api/v1/field-maps",
            json={**sample_fieldmap, "standard": "sigma", "source": "test"},
            headers=fm_admin_headers,
        )
        fm_client.post(
            "/api/v1/field-maps",
            json={
                **sample_fieldmap,
                "standard": "ecs",
                "source": None,
                "version": "8.11",
            },
            headers=fm_admin_headers,
        )
        resp = fm_client.get(
            "/api/v1/field-maps/group",
            params={"group_by": "standard"},
            headers=fm_admin_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data
        assert "total" in data
        assert "page" in data
        assert "per_page" in data
        items = data["items"]
        assert isinstance(items, list)
        standards = [g["standard"] for g in items if g.get("standard")]
        assert "sigma" in standards
        assert "ecs" in standards
        for g in items:
            assert "items" in g
            assert isinstance(g["items"], list)

    def test_list_grouped_sorts_groups_by_standard_asc(self, fm_client, fm_admin_headers, sample_fieldmap):
        fm_client.post("/api/v1/field-maps/seed", headers=fm_admin_headers)
        fm_client.post(
            "/api/v1/field-maps",
            json={**sample_fieldmap, "standard": "zebra", "source": "z"},
            headers=fm_admin_headers,
        )
        fm_client.post(
            "/api/v1/field-maps",
            json={**sample_fieldmap, "standard": "alpha", "source": "a"},
            headers=fm_admin_headers,
        )
        resp = fm_client.get(
            "/api/v1/field-maps/group",
            params={"group_by": "standard", "sort_order": "asc"},
            headers=fm_admin_headers,
        )
        assert resp.status_code == 200
        keys = [g["standard"] for g in resp.json()["items"] if g.get("standard")]
        assert keys == sorted(keys)

    def test_list_grouped_sorts_groups_by_version_desc(self, fm_client, fm_admin_headers, sample_fieldmap):
        fm_client.post("/api/v1/field-maps/seed", headers=fm_admin_headers)
        fm_client.post(
            "/api/v1/field-maps",
            json={**sample_fieldmap, "standard": "sigma", "version": "9.0.0", "source": None},
            headers=fm_admin_headers,
        )
        fm_client.post(
            "/api/v1/field-maps",
            json={**sample_fieldmap, "standard": "ecs", "version": "1.0.0", "source": None},
            headers=fm_admin_headers,
        )
        resp = fm_client.get(
            "/api/v1/field-maps/group",
            params={"group_by": "version", "sort_order": "desc"},
            headers=fm_admin_headers,
        )
        assert resp.status_code == 200
        keys = [g["version"] for g in resp.json()["items"] if g.get("version") is not None]
        assert keys == sorted(keys, reverse=True)

    def test_list_with_group_by_version(self, fm_client, fm_admin_headers, sample_fieldmap):
        fm_client.post("/api/v1/field-maps/seed", headers=fm_admin_headers)
        fm_client.post(
            "/api/v1/field-maps",
            json={**sample_fieldmap, "standard": "sigma", "version": "1.0.0"},
            headers=fm_admin_headers,
        )
        resp = fm_client.get(
            "/api/v1/field-maps/group",
            params={"group_by": "version"},
            headers=fm_admin_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data
        assert "total" in data
        items = data["items"]
        assert isinstance(items, list)
        for g in items:
            assert "items" in g
            assert isinstance(g["items"], list)


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
