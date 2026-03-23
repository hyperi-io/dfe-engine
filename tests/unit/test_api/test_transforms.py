"""Tests for the transforms API — WASM compile and test endpoints."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from dfe_engine.api.app import create_app
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
def app_with_transforms(tmp_path):
    """App with transform service URLs (unreachable for 503 tests)."""
    (tmp_path / "sources").mkdir(exist_ok=True)
    (tmp_path / "services").mkdir(exist_ok=True)
    settings = DFESettings(
        source=SourceSettings(sources_dir=str(tmp_path / "sources")),
        services=ServicesSettings(
            config_yaml_dir=str(tmp_path / "services"),
            transform_wasm_compiler_url="http://127.0.0.1:59999",
            transform_wasm_url="http://127.0.0.1:59998",
        ),
        auth=AuthSettings(
            enabled=True,
            local=LocalAuthSettings(
                enabled=True,
                admin_password="admin-pw",
                operator_password="op-pw",
                viewer_password="view-pw",
                org_id="test-org",
            ),
        ),
        api=APISettings(jwt_secret="test-secret"),
    )
    app = create_app(settings=settings)
    yield app
    _registries.clear()


@pytest.fixture
def transform_admin_headers(app_with_transforms):
    """Admin JWT for transforms app (must match app's jwt_secret)."""
    from dfe_engine.api.deps import create_access_token

    settings = app_with_transforms.state.settings
    token = create_access_token(
        data={"sub": "admin", "org_id": "test-org", "roles": ["admin"]},
        settings=settings,
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def transform_client(app_with_transforms, transform_admin_headers):
    """TestClient for transforms API."""
    with TestClient(app_with_transforms, raise_server_exceptions=False) as c:
        yield c


# ---------------------------------------------------------------------------
# Compile
# ---------------------------------------------------------------------------


class TestCompileAuth:
    def test_compile_requires_auth(self, transform_client: TestClient):
        resp = transform_client.post(
            "/api/v1/transforms/compile",
            json={"language": "rust", "files": {"lib.rs": "fn main() {}"}},
        )
        assert resp.status_code == 401

    def test_compile_viewer_forbidden(self, transform_client: TestClient):
        from dfe_engine.api.deps import create_access_token
        from dfe_engine.settings import APISettings, DFESettings

        settings = DFESettings(api=APISettings(jwt_secret="test-secret"))
        token = create_access_token(
            data={"sub": "viewer", "org_id": "test-org", "roles": ["viewer"]},
            settings=settings,
        )
        resp = transform_client.post(
            "/api/v1/transforms/compile",
            json={"language": "rust", "files": {"lib.rs": "fn main() {}"}},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 403


class TestCompileValidation:
    def test_compile_invalid_language(
        self, transform_client: TestClient, transform_admin_headers: dict
    ):
        resp = transform_client.post(
            "/api/v1/transforms/compile",
            json={"language": "python", "files": {"main.py": "print(1)"}},
            headers=transform_admin_headers,
        )
        assert resp.status_code == 422

    def test_compile_missing_language(
        self, transform_client: TestClient, transform_admin_headers: dict
    ):
        resp = transform_client.post(
            "/api/v1/transforms/compile",
            json={"files": {"lib.rs": "fn main() {}"}},
            headers=transform_admin_headers,
        )
        assert resp.status_code == 422

    def test_compile_invalid_files_value_type(
        self, transform_client: TestClient, transform_admin_headers: dict
    ):
        """files must be dict[str, str], not dict of lists."""
        resp = transform_client.post(
            "/api/v1/transforms/compile",
            json={"language": "rust", "files": {"lib.rs": ["invalid"]}},
            headers=transform_admin_headers,
        )
        assert resp.status_code == 422


class TestCompileServiceUnavailable:
    def test_compile_503_when_compiler_unreachable(
        self, transform_client: TestClient, transform_admin_headers: dict
    ):
        """Compiler URL points to closed port → ConnectError → 503."""
        resp = transform_client.post(
            "/api/v1/transforms/compile",
            json={"language": "rust", "files": {"lib.rs": "fn main() {}"}},
            headers=transform_admin_headers,
        )
        assert resp.status_code == 503
        data = resp.json()
        assert data.get("code") == "COMPILER_UNAVAILABLE"


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


class TestTestAuth:
    def test_test_requires_auth(self, transform_client: TestClient):
        resp = transform_client.post(
            "/api/v1/transforms/test",
            json={
                "wasm_base64": "dGVzdA==",
                "records": [{"key": "k", "value": "v"}],
            },
        )
        assert resp.status_code == 401

    def test_test_viewer_forbidden(self, transform_client: TestClient):
        from dfe_engine.api.deps import create_access_token
        from dfe_engine.settings import APISettings, DFESettings

        settings = DFESettings(api=APISettings(jwt_secret="test-secret"))
        token = create_access_token(
            data={"sub": "viewer", "org_id": "test-org", "roles": ["viewer"]},
            settings=settings,
        )
        resp = transform_client.post(
            "/api/v1/transforms/test",
            json={
                "wasm_base64": "dGVzdA==",
                "records": [{"key": "k", "value": "v"}],
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 403


class TestTestValidation:
    def test_test_missing_wasm_base64(
        self, transform_client: TestClient, transform_admin_headers: dict
    ):
        resp = transform_client.post(
            "/api/v1/transforms/test",
            json={"records": [{"key": "k", "value": "v"}]},
            headers=transform_admin_headers,
        )
        assert resp.status_code == 422

    def test_test_invalid_record_missing_value(
        self, transform_client: TestClient, transform_admin_headers: dict
    ):
        """SampleRecord requires 'value'."""
        resp = transform_client.post(
            "/api/v1/transforms/test",
            json={"wasm_base64": "dGVzdA==", "records": [{"key": "k"}]},
            headers=transform_admin_headers,
        )
        assert resp.status_code == 422

    def test_test_invalid_source_format(
        self, transform_client: TestClient, transform_admin_headers: dict
    ):
        resp = transform_client.post(
            "/api/v1/transforms/test",
            json={
                "wasm_base64": "dGVzdA==",
                "records": [{"key": "k", "value": "v"}],
                "source_format": "csv",
            },
            headers=transform_admin_headers,
        )
        assert resp.status_code == 422


class TestTestServiceUnavailable:
    def test_test_503_when_wasm_host_unreachable(
        self, transform_client: TestClient, transform_admin_headers: dict
    ):
        """WASM URL points to closed port → ConnectError → 503."""
        resp = transform_client.post(
            "/api/v1/transforms/test",
            json={
                "wasm_base64": "dGVzdA==",
                "records": [{"key": "k", "value": "v"}],
            },
            headers=transform_admin_headers,
        )
        assert resp.status_code == 503
        data = resp.json()
        assert data.get("code") == "TRANSFORM_HOST_UNAVAILABLE"
