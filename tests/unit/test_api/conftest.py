"""Shared fixtures for API tests.

Uses FastAPI TestClient with tmp_path isolation for registries.
Auth stores are bootstrapped via bootstrap_auth() with test accounts.
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
    auth_dir = tmp_path / "auth"
    auth_dir.mkdir()

    return DFESettings(
        config_dir=str(tmp_path),
        source=SourceSettings(sources_dir=str(sources_dir)),
        services=ServicesSettings(config_yaml_dir=str(services_dir)),
        auth=AuthSettings(
            enabled=True,
            auth_dir=str(auth_dir),
        ),
        api=APISettings(
            jwt_secret="test-secret-key-for-unit-tests",
            jwt_expire_minutes=30,
        ),
    )


@pytest.fixture
def app(api_settings: DFESettings):
    """Create a FastAPI app with test settings.

    After creation, bootstrap auth with test accounts so the login
    endpoint works against store-backed accounts.
    """
    application = create_app(settings=api_settings)

    # The lifespan will call bootstrap_auth, but we need to also seed
    # extra test accounts (operator, viewer) that aren't created by
    # the default bootstrap (which only seeds admin).
    # We do this inside the TestClient context (after lifespan runs).
    yield application
    # Cleanup registries after test
    _registries.clear()


@pytest.fixture
def client(app, api_settings: DFESettings) -> TestClient:
    """TestClient with lifespan events (registries bootstrapped)."""
    with TestClient(app, raise_server_exceptions=False) as c:
        # After lifespan runs, bootstrap_auth has created admin with
        # default "changeme" password. Now add operator + viewer accounts
        # and reset admin password to match test expectations.
        account_store = app.state.account_store
        group_store = app.state.group_store

        # Reset admin password to test password
        account_store.reset_password("admin", "test-admin-pw")

        # Create operator account (data_analyst + infra_admin)
        if account_store.get("operator") is None:
            account_store.create(
                "operator",
                "test-operator-pw",
                groups=["dfe-analysts", "dfe-infra"],
            )
            group_store.add_member("dfe-analysts", "operator")
            group_store.add_member("dfe-infra", "operator")

        # Create viewer account (data_viewer)
        if account_store.get("viewer") is None:
            account_store.create(
                "viewer",
                "test-viewer-pw",
                groups=["dfe-viewers"],
            )
            group_store.add_member("dfe-viewers", "viewer")

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
        "match": {"field": "tags.collector.type", "value": "test_source"},
        "header": {"type": "time_series", "version": "1.0.0"},
        "schema_config": {"engine": "MergeTree"},
    }
