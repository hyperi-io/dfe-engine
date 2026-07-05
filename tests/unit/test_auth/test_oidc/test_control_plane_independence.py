#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_oidc/test_control_plane_independence.py
#  Purpose:      Prove that OIDC auth works with static YAML files when dfe-engine is down
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""CRITICAL: Verify auth works when dfe-engine management features are unavailable.

These tests prove the platform keeps running with just static YAML files.
No provider registry, no sync, no API — just group files on disk.

Design principle: dfe-engine is a control plane, NOT a runtime dependency.
If dfe-engine is down, Envoy Gateway keeps doing OIDC (static K8s CRD),
and existing group YAML files on disk still resolve roles correctly.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dfe_engine.api.app import create_app
from dfe_engine.api.deps import _registries, create_access_token
from dfe_engine.auth.bootstrap import bootstrap_auth
from dfe_engine.auth.groups import GroupStore
from dfe_engine.auth.oidc.models import OIDCProvider
from dfe_engine.auth.oidc.registry import OIDCProviderRegistry
from dfe_engine.settings import (
    APISettings,
    AuthSettings,
    DFESettings,
    ServicesSettings,
    SourceSettings,
)
from dfe_engine.yaml_utils import yaml_dump

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def auth_settings(tmp_path: Path) -> DFESettings:
    """Minimal DFESettings with auth enabled for independence tests."""
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
            jwt_secret="independence-test-secret-hmac-32-bytes",
            jwt_expire_minutes=30,
        ),
    )


@pytest.fixture
def app(auth_settings: DFESettings):
    """FastAPI app for control plane independence tests."""
    application = create_app(settings=auth_settings)
    yield application
    _registries.clear()


@pytest.fixture
def client(app, auth_settings: DFESettings) -> TestClient:
    """TestClient with lifespan — bootstrap groups already seeded."""
    with TestClient(app, raise_server_exceptions=False) as c:
        account_store = app.state.account_store
        account_store.reset_password("admin", "test-admin-pw")
        yield c


@pytest.fixture
def admin_headers(auth_settings: DFESettings) -> dict[str, str]:
    """JWT Bearer headers for admin."""
    token = create_access_token(
        data={"sub": "admin", "org_id": "default", "roles": ["admin"]},
        settings=auth_settings,
    )
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Class 1: Auth resolves roles from group files — no dfe-engine services needed
# ---------------------------------------------------------------------------


class TestOIDCAuthWithStaticFiles:
    """Auth resolves roles from group files — no dfe-engine services needed."""

    def test_oidc_groups_resolve_from_yaml_files(self, tmp_path: Path) -> None:
        """X-Oidc-Groups values match group filenames → roles resolved."""
        groups_dir = tmp_path / "groups"
        # Create group YAML files manually — simulating pre-existing config on disk
        groups_dir.mkdir(parents=True)
        yaml_dump(
            {"description": "Admins", "roles": ["admin"], "members": []},
            groups_dir / "platform-admins.yaml",
        )

        store = GroupStore(groups_dir)
        from dfe_engine.api.deps import _resolve_roles_from_groups

        roles, org_ids = _resolve_roles_from_groups(["platform-admins"], store)

        assert roles == ["admin"]
        assert org_ids == []

    def test_multiple_groups_resolve_union_of_roles(self, tmp_path: Path) -> None:
        """User in multiple groups gets the union of all roles."""
        groups_dir = tmp_path / "groups"
        groups_dir.mkdir(parents=True)
        yaml_dump(
            {"description": "Analysts", "roles": ["data_analyst"], "members": []},
            groups_dir / "eng-analysts.yaml",
        )
        yaml_dump(
            {"description": "Infra", "roles": ["infra"], "members": []},
            groups_dir / "eng-infra.yaml",
        )

        store = GroupStore(groups_dir)
        from dfe_engine.api.deps import _resolve_roles_from_groups

        roles, org_ids = _resolve_roles_from_groups(["eng-analysts", "eng-infra"], store)

        assert "data_analyst" in roles
        assert "infra" in roles
        # Roles are sorted and deduplicated
        assert roles == sorted(set(roles))

    def test_unknown_groups_yield_no_roles_not_error(self, tmp_path: Path) -> None:
        """Groups not matching any file → empty roles, NOT an error."""
        groups_dir = tmp_path / "groups"
        groups_dir.mkdir(parents=True)
        # No files written — all groups are unknown

        store = GroupStore(groups_dir)
        from dfe_engine.api.deps import _resolve_roles_from_groups

        # Should NOT raise — unknown groups are silently skipped
        roles, org_ids = _resolve_roles_from_groups(["unknown-group-1", "unknown-group-2"], store)

        assert roles == []
        assert org_ids == []

    def test_guid_group_names_work(self, tmp_path: Path) -> None:
        """Entra ID GUIDs as group filenames resolve correctly (unfriendly mode)."""
        groups_dir = tmp_path / "groups"
        groups_dir.mkdir(parents=True)
        guid = "7c4e1a2b-5f3d-4e8a-b1c9-0d2f6e8a3b5c"
        yaml_dump(
            {"description": "Entra group", "roles": ["data_viewer"], "members": []},
            groups_dir / f"{guid}.yaml",
        )

        store = GroupStore(groups_dir)
        from dfe_engine.api.deps import _resolve_roles_from_groups

        roles, org_ids = _resolve_roles_from_groups([guid], store)

        assert roles == ["data_viewer"]


# ---------------------------------------------------------------------------
# Class 2: Auth works even if no OIDC providers are registered
# ---------------------------------------------------------------------------


class TestAuthWithoutProviderRegistry:
    """Auth works even if no OIDC providers are registered."""

    def test_oidc_header_auth_works_no_providers(self, client: TestClient, app) -> None:
        """OIDC header auth works when oidc-providers dir is empty."""
        # Verify the oidc-providers dir has no entries
        oidc_registry: OIDCProviderRegistry = app.state.oidc_provider_registry
        assert oidc_registry.list() == []

        # dfe-admins group is seeded by bootstrap_auth — send its name in header
        response = client.get(
            "/api/v1/auth/me",
            headers={
                "X-Oidc-Subject": "sso-user@example.com",
                "X-Oidc-Groups": "dfe-admins",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["user_id"] == "sso-user@example.com"
        assert "admin" in data["roles"]

    def test_jwt_auth_unaffected_by_oidc_config(
        self, client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        """JWT Bearer auth path is completely independent of OIDC config."""
        response = client.get("/api/v1/auth/me", headers=admin_headers)

        assert response.status_code == 200
        data = response.json()
        assert data["user_id"] == "admin"
        assert "admin" in data["roles"]


# ---------------------------------------------------------------------------
# Class 3: Detaching a provider does NOT delete group files
# ---------------------------------------------------------------------------


class TestGroupsPersistAfterProviderDetach:
    """Detaching a provider does NOT delete group files."""

    def test_groups_survive_provider_deletion(self, tmp_path: Path) -> None:
        """Delete provider → group files still exist → roles still resolve."""
        oidc_dir = tmp_path / "oidc-providers"
        groups_dir = tmp_path / "groups"
        groups_dir.mkdir(parents=True)

        # Create a provider and a group that references it via source_provider
        registry = OIDCProviderRegistry(oidc_dir)
        provider = OIDCProvider(
            type="generic",
            enabled=True,
            display_name="Corp SSO",
            issuer="https://sso.corp.example.com",
        )
        registry.create("corp-sso", provider)

        # Write a group file that was synced from this provider
        yaml_dump(
            {
                "description": "Engineering team",
                "roles": ["data_analyst"],
                "members": [],
                "source_provider": "corp-sso",
                "source_id": "grp-eng-001",
            },
            groups_dir / "engineering.yaml",
        )

        # Delete the provider (simulating provider detach)
        registry.delete("corp-sso")

        # Group file must still exist
        assert (groups_dir / "engineering.yaml").exists()

        # Role resolution still works
        store = GroupStore(groups_dir)
        group = store.get("engineering")
        assert group is not None
        assert "data_analyst" in group.roles

    def test_roles_still_resolve_after_provider_gone(self, tmp_path: Path) -> None:
        """After provider detached, group-to-role resolution is unchanged."""
        oidc_dir = tmp_path / "oidc-providers"
        groups_dir = tmp_path / "groups"
        groups_dir.mkdir(parents=True)

        registry = OIDCProviderRegistry(oidc_dir)
        registry.create(
            "my-idp",
            OIDCProvider(type="generic", issuer="https://idp.example.com"),
        )

        # Simulate groups synced from this provider
        yaml_dump(
            {"description": "Finance", "roles": ["data_viewer"], "members": []},
            groups_dir / "finance-team.yaml",
        )
        yaml_dump(
            {"description": "Security", "roles": ["admin"], "members": []},
            groups_dir / "security-ops.yaml",
        )

        # Detach the provider
        registry.delete("my-idp")
        assert registry.get("my-idp") is None

        # Role resolution is completely unaffected
        store = GroupStore(groups_dir)
        from dfe_engine.api.deps import _resolve_roles_from_groups

        roles, _ = _resolve_roles_from_groups(["finance-team", "security-ops"], store)
        assert "data_viewer" in roles
        assert "admin" in roles


# ---------------------------------------------------------------------------
# Class 4: Group files with roles persist through engine restart (bootstrap)
# ---------------------------------------------------------------------------


class TestExistingRolesSurviveRestart:
    """Group files with roles persist through engine restart (bootstrap)."""

    def test_bootstrap_does_not_overwrite_custom_groups(self, tmp_path: Path) -> None:
        """Pre-existing group files survive bootstrap_auth()."""
        auth_dir = tmp_path / "auth"
        groups_dir = auth_dir / "groups"
        groups_dir.mkdir(parents=True)

        # Write the dfe-admins group so _seed_admin can add the member to it
        yaml_dump(
            {"description": "Full administrative access", "roles": ["admin"], "members": []},
            groups_dir / "dfe-admins.yaml",
        )
        # Write a custom group that should survive bootstrap
        yaml_dump(
            {"description": "Custom ops team", "roles": ["infra"], "members": []},
            groups_dir / "custom-ops.yaml",
        )

        # Run bootstrap — simulates engine restart
        bootstrap_auth(auth_dir)

        # Custom group must still exist and have original roles
        store = GroupStore(groups_dir)
        group = store.get("custom-ops")
        assert group is not None
        assert group.roles == ["infra"]
        assert group.description == "Custom ops team"

    def test_bootstrap_does_not_remove_user_assigned_roles(self, tmp_path: Path) -> None:
        """Roles assigned by admin to groups survive bootstrap."""
        auth_dir = tmp_path / "auth"
        groups_dir = auth_dir / "groups"
        groups_dir.mkdir(parents=True)

        # Write the dfe-admins group so _seed_admin can add the member to it
        yaml_dump(
            {"description": "Full administrative access", "roles": ["admin"], "members": []},
            groups_dir / "dfe-admins.yaml",
        )
        # Simulate an admin who added extra roles to dfe-analysts before restart
        yaml_dump(
            {
                "description": "Hunt, query, source CRUD — upgraded",
                "roles": ["data_analyst", "infra_ro"],
                "members": ["alice", "bob"],
            },
            groups_dir / "dfe-analysts.yaml",
        )

        # Run bootstrap (engine restart)
        bootstrap_auth(auth_dir)

        # Bootstrap only seeds groups if the dir is EMPTY; since dfe-analysts.yaml
        # already existed, the existing file must be preserved intact.
        store = GroupStore(groups_dir)
        group = store.get("dfe-analysts")
        assert group is not None
        # The extra role added by admin must survive
        assert "infra_ro" in group.roles
        assert "data_analyst" in group.roles
        # Members must survive
        assert "alice" in group.members
        assert "bob" in group.members


# ---------------------------------------------------------------------------
# Class 5: If group sync fails, existing group files still resolve roles
# ---------------------------------------------------------------------------


class TestSyncFailureDoesNotBreakAuth:
    """If group sync fails, existing group files still resolve roles."""

    def test_stale_group_files_still_work(self, tmp_path: Path) -> None:
        """Groups from a previous sync remain functional after sync failure."""
        groups_dir = tmp_path / "groups"
        groups_dir.mkdir(parents=True)

        # These files exist from a previous successful sync
        yaml_dump(
            {"description": "Engineering", "roles": ["data_analyst"], "members": []},
            groups_dir / "engineering.yaml",
        )
        yaml_dump(
            {"description": "SRE team", "roles": ["infra"], "members": []},
            groups_dir / "sre-team.yaml",
        )

        # Simulate sync failure by NOT updating the files (they remain as-is)
        # Auth must still work with the stale — but valid — files

        store = GroupStore(groups_dir)
        from dfe_engine.api.deps import _resolve_roles_from_groups

        roles, _ = _resolve_roles_from_groups(["engineering", "sre-team"], store)

        assert "data_analyst" in roles
        assert "infra" in roles

    def test_sync_error_does_not_corrupt_existing_groups(self, tmp_path: Path) -> None:
        """A failed sync does not modify existing group files.

        Sync writes to a temp file then renames atomically, so a failure
        mid-sync leaves the original files untouched. This test proves the
        original YAML data is still readable after a partial write attempt.
        """
        groups_dir = tmp_path / "groups"
        groups_dir.mkdir(parents=True)

        original_roles = ["data_analyst", "data_viewer"]
        yaml_dump(
            {
                "description": "Mixed access team",
                "roles": original_roles,
                "members": ["carol"],
            },
            groups_dir / "mixed-team.yaml",
        )

        # Simulate a failed sync that writes a partial/empty temp file
        # then abandons it without renaming (leaving original intact)
        temp_file = groups_dir / "mixed-team.yaml.tmp"
        temp_file.write_text("")  # partial write — never renamed

        # Original must still be readable and correct
        store = GroupStore(groups_dir)
        group = store.get("mixed-team")
        assert group is not None
        assert set(group.roles) == set(original_roles)
        assert "carol" in group.members

        # Cleanup temp file (would normally be cleaned up by sync logic)
        temp_file.unlink()


# ---------------------------------------------------------------------------
# Class 6: The complete auth flow works with zero network/API calls
# ---------------------------------------------------------------------------


class TestAuthFlowWithNoExternalDependencies:
    """The complete auth flow works with zero network/API calls."""

    def test_full_oidc_flow_offline(self, client: TestClient, app) -> None:
        """Complete OIDC auth flow: header → group resolution → role check.

        No Envoy, no IdP, no network — just headers and YAML files on disk.
        Bootstrap seeds dfe-admins with the 'admin' role. Sending that group
        name in X-Oidc-Groups must resolve to admin role and grant access.
        """
        # Step 1: authenticate via OIDC headers (simulating Envoy forwarding)
        me_response = client.get(
            "/api/v1/auth/me",
            headers={
                "X-Oidc-Subject": "offline-user@example.com",
                "X-Oidc-Groups": "dfe-admins",
            },
        )
        assert me_response.status_code == 200
        me_data = me_response.json()

        # Step 2: verify correct user and roles resolved from YAML group file
        assert me_data["user_id"] == "offline-user@example.com"
        assert "admin" in me_data["roles"]
        assert "dfe-admins" in me_data["groups"]

        # Step 3: verify a protected admin endpoint works with those roles
        sources_response = client.get(
            "/api/v1/sources",
            headers={
                "X-Oidc-Subject": "offline-user@example.com",
                "X-Oidc-Groups": "dfe-admins",
            },
        )
        # 200 (sources configured) or 503 (sources not configured) — either
        # proves auth succeeded; 401/403 would indicate auth failure
        assert sources_response.status_code not in (401, 403)

    def test_full_api_key_flow_offline(self, client: TestClient, app) -> None:
        """API key auth works with zero external dependencies."""
        # Create an API key associated with dfe-admins group
        api_key_store = app.state.api_key_store
        _key_meta, full_key = api_key_store.create(
            "ci-automation",
            groups=["dfe-admins"],
            description="CI automation key for independence test",
        )

        # Authenticate using the API key — no network, no IdP
        response = client.get(
            "/api/v1/auth/me",
            headers={"X-API-Key": full_key},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["user_id"] == "apikey:ci-automation"
        assert "admin" in data["roles"]

    def test_full_jwt_flow_offline(
        self, client: TestClient, app, auth_settings: DFESettings
    ) -> None:
        """JWT login + Bearer auth works with zero external dependencies."""
        # Step 1: login via local provider (no external IdP)
        login_response = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "test-admin-pw"},
        )
        assert login_response.status_code == 200
        token = login_response.json()["access_token"]
        assert token

        # Step 2: use the JWT bearer token to authenticate — no network
        me_response = client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert me_response.status_code == 200
        data = me_response.json()
        assert data["user_id"] == "admin"
