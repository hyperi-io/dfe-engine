"""Tests for the deployments router — DeploymentConfigRegistry CRUD + sizing."""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app_with_deployments(tmp_path):
    """App fixture with deployment config registry initialized."""
    from dfe_engine.api.app import create_app
    from dfe_engine.api.deps import _registries
    from dfe_engine.settings import (
        APISettings,
        AuthSettings,
        DeploymentSettings,
        DFESettings,
        HuntsSettings,
        ServicesSettings,
        SourceSettings,
    )

    deploy_dir = tmp_path / "deployment"
    deploy_dir.mkdir()

    settings = DFESettings(
        config_dir=str(tmp_path),
        source=SourceSettings(sources_dir=str(tmp_path / "sources")),
        services=ServicesSettings(config_yaml_dir=str(tmp_path / "services")),
        deployment=DeploymentSettings(config_dir=str(deploy_dir)),
        hunts=HuntsSettings(alert_destinations_dir=str(tmp_path / "alerts")),
        auth=AuthSettings(
            enabled=True,
            auth_dir=str(tmp_path / "auth"),
        ),
        api=APISettings(jwt_secret="test-secret"),
    )
    for d in ["sources", "services", "alerts"]:
        (tmp_path / d).mkdir(exist_ok=True)

    app = create_app(settings=settings)
    yield app
    _registries.clear()


@pytest.fixture
def deploy_client(app_with_deployments):
    with TestClient(app_with_deployments, raise_server_exceptions=False) as c:
        account_store = app_with_deployments.state.account_store
        group_store = app_with_deployments.state.group_store
        if account_store.get("viewer") is None:
            account_store.create("viewer", "test-viewer-pw", groups=["dfe-viewers"])
            group_store.add_member("dfe-viewers", "viewer")
        group_store.update(
            "dfe-viewers",
            roles=["infra_viewer", "data_analyst_viewer", "data_viewer"],
        )
        yield c


@pytest.fixture
def deploy_admin_headers(app_with_deployments):
    from dfe_engine.api.deps import create_access_token
    from dfe_engine.settings import APISettings, DFESettings

    settings = DFESettings(api=APISettings(jwt_secret="test-secret"))
    token = create_access_token(
        data={"sub": "admin", "org_id": "test-org", "roles": ["admin"]},
        settings=settings,
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def sample_deployment():
    return {
        "size": "small",
        "replicas": 2,
        "image": "harbor.hyperi.io/dfe/dfe-receiver",
        "image_tag": "1.0.0",
        "keda": {
            "enabled": True,
            "min_replicas": 1,
            "max_replicas": 5,
            "polling_interval": 30,
            "cooldown_period": 300,
        },
    }


class TestDeploymentsList:
    def test_list_empty(self, deploy_client, deploy_admin_headers):
        resp = deploy_client.get("/api/v1/deployments", headers=deploy_admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["items"] == []
        assert data["total"] == 0

    def test_list_requires_auth(self, deploy_client):
        resp = deploy_client.get("/api/v1/deployments")
        assert resp.status_code == 401


class TestDeploymentsCRUD:
    def test_save_and_get(self, deploy_client, deploy_admin_headers, sample_deployment):
        # Save
        resp = deploy_client.put(
            "/api/v1/deployments/receiver/default",
            json=sample_deployment,
            headers=deploy_admin_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["message"] == "saved"

        # Get
        resp = deploy_client.get(
            "/api/v1/deployments/receiver/default",
            headers=deploy_admin_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["service"] == "receiver"
        assert data["instance"] == "default"
        assert data["config"]["replicas"] == 2
        assert data["config"]["keda"]["enabled"] is True

    def test_get_not_found(self, deploy_client, deploy_admin_headers):
        resp = deploy_client.get(
            "/api/v1/deployments/receiver/nonexistent",
            headers=deploy_admin_headers,
        )
        assert resp.status_code == 404
        assert resp.json()["code"] == "not_found"

    def test_delete_deployment(self, deploy_client, deploy_admin_headers, sample_deployment):
        deploy_client.put(
            "/api/v1/deployments/loader/default",
            json=sample_deployment,
            headers=deploy_admin_headers,
        )
        resp = deploy_client.delete(
            "/api/v1/deployments/loader/default",
            headers=deploy_admin_headers,
        )
        assert resp.status_code == 204

        resp = deploy_client.get(
            "/api/v1/deployments/loader/default",
            headers=deploy_admin_headers,
        )
        assert resp.status_code == 404

    def test_delete_not_found(self, deploy_client, deploy_admin_headers):
        resp = deploy_client.delete(
            "/api/v1/deployments/receiver/ghost",
            headers=deploy_admin_headers,
        )
        assert resp.status_code == 404

    def test_list_after_save(self, deploy_client, deploy_admin_headers, sample_deployment):
        deploy_client.put(
            "/api/v1/deployments/receiver/production",
            json=sample_deployment,
            headers=deploy_admin_headers,
        )
        resp = deploy_client.get("/api/v1/deployments", headers=deploy_admin_headers)
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["service"] == "receiver"
        assert data["items"][0]["instance"] == "production"

    def test_unknown_service_schema_less(self, deploy_client, deploy_admin_headers):
        """Unknown services store raw dicts without typed validation."""
        body = {"custom_key": "custom_value", "scaling": {"max": 10}}
        resp = deploy_client.put(
            "/api/v1/deployments/my-new-service/default",
            json=body,
            headers=deploy_admin_headers,
        )
        assert resp.status_code == 200

        resp = deploy_client.get(
            "/api/v1/deployments/my-new-service/default",
            headers=deploy_admin_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["service"] == "my-new-service"
        assert data["config"]["custom_key"] == "custom_value"

    def test_viewer_read_allowed(self, deploy_client, deploy_admin_headers):
        from dfe_engine.api.deps import create_access_token
        from dfe_engine.settings import APISettings, DFESettings

        settings = DFESettings(api=APISettings(jwt_secret="test-secret"))
        token = create_access_token(
            data={"sub": "viewer", "org_id": "test-org", "roles": ["infra_viewer"]},
            settings=settings,
        )
        viewer_headers = {"Authorization": f"Bearer {token}"}
        resp = deploy_client.get("/api/v1/deployments", headers=viewer_headers)
        assert resp.status_code == 200

    def test_viewer_write_forbidden(self, deploy_client, sample_deployment):
        from dfe_engine.api.deps import create_access_token
        from dfe_engine.settings import APISettings, DFESettings

        settings = DFESettings(api=APISettings(jwt_secret="test-secret"))
        token = create_access_token(
            data={"sub": "viewer", "org_id": "test-org", "roles": ["infra_viewer"]},
            settings=settings,
        )
        viewer_headers = {"Authorization": f"Bearer {token}"}
        resp = deploy_client.put(
            "/api/v1/deployments/receiver/default",
            json=sample_deployment,
            headers=viewer_headers,
        )
        assert resp.status_code == 403


class TestDeploymentSizing:
    def test_apply_valid_size(self, deploy_client, deploy_admin_headers):
        resp = deploy_client.post(
            "/api/v1/deployments/receiver/default/size/small",
            headers=deploy_admin_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["service"] == "receiver"
        assert data["size"] == "small"
        assert "service_config_overrides" in data

    def test_apply_invalid_size(self, deploy_client, deploy_admin_headers):
        resp = deploy_client.post(
            "/api/v1/deployments/receiver/default/size/enormous",
            headers=deploy_admin_headers,
        )
        assert resp.status_code == 422
        assert resp.json()["code"] == "invalid_size"

    def test_seed(self, deploy_client, deploy_admin_headers):
        resp = deploy_client.post(
            "/api/v1/deployments/seed",
            headers=deploy_admin_headers,
        )
        assert resp.status_code == 200
        assert "seeded" in resp.json()
