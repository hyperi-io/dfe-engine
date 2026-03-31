"""Shared fixtures for API tests.

Uses FastAPI TestClient with tmp_path isolation for SourceRegistry.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dfe_engine.api.app import create_app
from dfe_engine.api.deps import _registries, create_access_token
from dfe_engine.settings import (
    APISettings,
    AuthSettings,
    DFESettings,
    LocalAuthSettings,
    ServicesSettings,
    SourceSettings,
)


@pytest.fixture
def api_settings(tmp_path: Path) -> DFESettings:
    """Minimal DFESettings for API tests with tmp_path isolation."""
    sources_dir = tmp_path / "sources"
    sources_dir.mkdir()
    services_dir = tmp_path / "services"
    services_dir.mkdir()

    return DFESettings(
        source=SourceSettings(sources_dir=str(sources_dir)),
        services=ServicesSettings(config_yaml_dir=str(services_dir)),
        auth=AuthSettings(
            enabled=True,
            local=LocalAuthSettings(
                enabled=True,
                admin_password="test-admin-pw",
                operator_password="test-operator-pw",
                viewer_password="test-viewer-pw",
                org_id="test-org",
            ),
        ),
        api=APISettings(
            jwt_secret="test-secret-key-for-unit-tests",
            jwt_expire_minutes=30,
        ),
    )


@pytest.fixture
def app(api_settings: DFESettings):
    """Create a FastAPI app with test settings."""
    application = create_app(settings=api_settings)
    yield application
    # Cleanup registries after test
    _registries.clear()


@pytest.fixture
def client(app) -> TestClient:
    """TestClient with lifespan events (registries bootstrapped)."""
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def admin_token(api_settings: DFESettings) -> str:
    """JWT token for admin user."""
    return create_access_token(
        data={"sub": "admin", "org_id": "test-org", "roles": ["admin"]},
        settings=api_settings,
    )


@pytest.fixture
def viewer_token(api_settings: DFESettings) -> str:
    """JWT token for viewer user (read-only).

    Uses infra_viewer (config:read) + data_analyst_viewer (source:read)
    to cover all read-only API tests without granting write access.
    """
    return create_access_token(
        data={
            "sub": "viewer",
            "org_id": "test-org",
            "roles": ["infra_viewer", "data_analyst_viewer"],
        },
        settings=api_settings,
    )


@pytest.fixture
def admin_headers(admin_token: str) -> dict[str, str]:
    """Authorization headers for admin user."""
    return {"Authorization": f"Bearer {admin_token}"}


@pytest.fixture
def viewer_headers(viewer_token: str) -> dict[str, str]:
    """Authorization headers for viewer user."""
    return {"Authorization": f"Bearer {viewer_token}"}


@pytest.fixture
def sample_source() -> dict:
    """A minimal valid source definition for testing."""
    return {
        "source": "test_source",
        "display_name": "Test Source",
        "description": "A test data source",
        "enabled": True,
        "header": {"type": "time_series", "version": "1.0.0"},
        "schema_config": {"engine": "MergeTree"},
    }
