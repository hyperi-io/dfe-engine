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
    ClickHouseSettings,
    DFESettings,
    HuntsSettings,
    LocalAuthSettings,
    SchemasSettings,
    SecretsSettings,
    ServicesSettings,
    SourceSettings,
)
from tests.support.core_sources import write_landing_definition

# The admin password these fixtures inject, as a deployment's secret store would.
ADMIN_PASSWORD = "test-admin-pw"


@pytest.fixture
def api_settings(tmp_path: Path) -> DFESettings:
    """Minimal DFESettings for API tests with tmp_path isolation."""
    sources_dir = tmp_path / "sources"
    sources_dir.mkdir()
    services_dir = tmp_path / "services"
    services_dir.mkdir()
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    hunts_dir = tmp_path / "hunts"
    hunts_dir.mkdir()
    auth_dir = tmp_path / "auth"
    auth_dir.mkdir()
    schemas_dir = tmp_path / "schemas"
    schemas_dir.mkdir()
    # Stands in for the image-baked schema seed: without it the app seeds no
    # landing source, and every test that expects `main` to exist fails.
    write_landing_definition(schemas_dir=schemas_dir)
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()

    return DFESettings(
        config_dir=str(tmp_path),
        # API unit tests are hermetic - they exercise the API/auth layer over
        # tmp_path YAML stores and never touch the data plane. Skip the startup
        # ClickHouse table bootstrap so the app lifespan does not block on a
        # ClickHouse connection (integration tests use the real tiered ch fixture).
        clickhouse=ClickHouseSettings(bootstrap_tables=False),
        source=SourceSettings(sources_dir=str(sources_dir)),
        services=ServicesSettings(config_yaml_dir=str(services_dir)),
        schemas=SchemasSettings(schemas_dir=str(schemas_dir)),
        hunts=HuntsSettings(rules_dir=str(rules_dir), hunt_dir=str(hunts_dir)),
        auth=AuthSettings(
            enabled=True,
            auth_dir=str(auth_dir),
            # Simulate the Envoy-fronted production deployment: trust the
            # X-Oidc-* identity headers (auth Path 1). Production default is off.
            trust_proxy_auth_headers=True,
            # These fixtures run the production posture, which refuses to start on
            # the shipped admin password -- so inject one, as a deployment does.
            local=LocalAuthSettings(admin_password=ADMIN_PASSWORD),
        ),
        secrets=SecretsSettings(provider="file", path=str(secrets_dir)),
        api=APISettings(
            jwt_secret="test-secret-key-for-unit-tests-hmac32",
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
        account_store.reset_password("admin", ADMIN_PASSWORD)

        # Create operator account (data_analyst + infra_admin)
        if account_store.get("operator") is None:
            account_store.create(
                "operator",
                "test-operator-pw",
                groups=["dfe-analysts", "dfe-infra"],
            )
            group_store.add_member("dfe-analysts", "operator")
            group_store.add_member("dfe-infra", "operator")

        # Create viewer account (read-only API tests via dfe-viewers group roles)
        if account_store.get("viewer") is None:
            account_store.create(
                "viewer",
                "test-viewer-pw",
                groups=["dfe-viewers"],
            )
            group_store.add_member("dfe-viewers", "viewer")
        group_store.update(
            "dfe-viewers",
            roles=["infra_viewer", "data_analyst_viewer", "data_viewer"],
        )

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

    Claims may be stale; get_current_user resolves roles from dfe-viewers membership.
    """
    return create_access_token(
        data={
            "sub": "viewer",
            "org_id": "test-org",
            "roles": ["data_viewer"],
            "groups": ["dfe-viewers"],
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
def operator_token(api_settings: DFESettings) -> str:
    """JWT for operator (data_analyst + infra_admin via group membership).

    infra_admin grants group:* but NOT role:* - so operator can manage groups
    yet cannot assign roles it does not itself hold. Roles resolve from group
    membership, so the claim here only carries the subject.
    """
    return create_access_token(
        data={"sub": "operator", "org_id": "test-org", "roles": ["infra_admin"]},
        settings=api_settings,
    )


@pytest.fixture
def operator_headers(operator_token: str) -> dict[str, str]:
    """Authorization headers for operator (group:write, not role:write)."""
    return {"Authorization": f"Bearer {operator_token}"}


@pytest.fixture
def sample_source() -> dict:
    """A minimal valid source definition for testing."""
    return {
        "source": "test-source",
        "display_name": "Test Source",
        "description": "A test data source",
        "enabled": True,
        "match": {"field": "tags.collector.type", "value": "test_source"},
        "header": {"type": "timeseries", "version": "1.0.0"},
        "schema_config": {"engine": "MergeTree"},
    }
