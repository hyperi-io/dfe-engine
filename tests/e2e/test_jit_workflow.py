#  Project:      dfe-engine
#  File:         tests/e2e/test_jit_workflow.py
#  Purpose:      E2E tests for JIT provisioning workflow on first OIDC login
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""E2E tests for JIT provisioning — shadow account creation on OIDC login."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from dfe_engine.api.app import create_app
from dfe_engine.api.deps import _registries
from dfe_engine.settings import (
    APISettings,
    AuthSettings,
    DFESettings,
    SchemasSettings,
    ServicesSettings,
    SourceSettings,
)


@pytest.fixture
def jit_settings(tmp_path):
    (tmp_path / "sources").mkdir()
    (tmp_path / "services").mkdir()
    (tmp_path / "auth").mkdir()
    (tmp_path / "schemas").mkdir()
    return DFESettings(
        config_dir=str(tmp_path),
        source=SourceSettings(sources_dir=str(tmp_path / "sources")),
        services=ServicesSettings(config_yaml_dir=str(tmp_path / "services")),
        schemas=SchemasSettings(schemas_dir=str(tmp_path / "schemas")),
        auth=AuthSettings(enabled=True, auth_dir=str(tmp_path / "auth")),
        api=APISettings(jwt_secret="jit-test-secret-key-32-chars-lo!"),
    )


@pytest.fixture
def jit_client(jit_settings):
    app = create_app(settings=jit_settings)
    with TestClient(app, raise_server_exceptions=False) as client:
        # Create a group with org_ids for testing
        group_store = app.state.group_store
        group_store.create("test-org-viewers", roles=["customer_viewer"])
        group_store.update("test-org-viewers", org_ids=["test-org"])
        yield client
    _registries.clear()


class TestJitWorkflow:
    def test_oidc_first_login_creates_shadow_account(self, jit_client):
        resp = jit_client.get(
            "/api/v1/auth/me",
            headers={
                "X-Oidc-Subject": "newuser@corp.com",
                "X-Oidc-Groups": "test-org-viewers",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["user_id"] == "newuser@corp.com"

        # Verify shadow account was created
        account_store = jit_client.app.state.account_store
        account = account_store.get("newuser-corp-com")
        assert account is not None
        assert account.external is True
        assert account.last_login_at != ""

    def test_oidc_subsequent_login_updates_timestamp(self, jit_client):
        headers = {
            "X-Oidc-Subject": "returning@corp.com",
            "X-Oidc-Groups": "test-org-viewers",
        }
        jit_client.get("/api/v1/auth/me", headers=headers)
        first = jit_client.app.state.account_store.get("returning-corp-com")
        jit_client.get("/api/v1/auth/me", headers=headers)
        second = jit_client.app.state.account_store.get("returning-corp-com")
        assert second.last_login_at >= first.last_login_at

    def test_oidc_auth_still_works_without_jit(self, jit_client):
        """Auth succeeds even if JIT provisioner not configured."""
        # Remove JIT provisioner
        jit_client.app.state.jit_provisioner = None
        resp = jit_client.get(
            "/api/v1/auth/me",
            headers={
                "X-Oidc-Subject": "nojit@corp.com",
                "X-Oidc-Groups": "test-org-viewers",
            },
        )
        assert resp.status_code == 200

    def test_oidc_groups_change_updates_account(self, jit_client):
        """When OIDC groups change, shadow account groups are updated."""
        # First login with test-org-viewers group
        jit_client.get(
            "/api/v1/auth/me",
            headers={
                "X-Oidc-Subject": "groupchange@corp.com",
                "X-Oidc-Groups": "test-org-viewers",
            },
        )

        # Create a second group
        group_store = jit_client.app.state.group_store
        if group_store.get("extra-group") is None:
            group_store.create("extra-group", roles=["data_viewer"])

        # Second login adds extra-group
        jit_client.get(
            "/api/v1/auth/me",
            headers={
                "X-Oidc-Subject": "groupchange@corp.com",
                "X-Oidc-Groups": "test-org-viewers,extra-group",
            },
        )

        account = jit_client.app.state.account_store.get("groupchange-corp-com")
        assert account is not None
        assert "extra-group" in account.groups

    def test_shadow_account_visible_in_accounts_api(self, jit_client):
        """Shadow accounts appear in the accounts list API."""
        # Trigger JIT creation
        jit_client.get(
            "/api/v1/auth/me",
            headers={
                "X-Oidc-Subject": "visible@corp.com",
                "X-Oidc-Groups": "test-org-viewers",
            },
        )

        # Login as admin and check accounts list
        jit_client.app.state.account_store.reset_password("admin", "test-admin-pw")
        login = jit_client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "test-admin-pw"},
        )
        assert login.status_code == 200
        admin_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

        resp = jit_client.get("/api/v1/auth/accounts", headers=admin_headers)
        assert resp.status_code == 200
        usernames = [a["username"] for a in resp.json()["items"]]
        assert "visible-corp-com" in usernames

    def test_external_flag_on_shadow_account(self, jit_client):
        """Shadow accounts have external=True."""
        jit_client.get(
            "/api/v1/auth/me",
            headers={
                "X-Oidc-Subject": "extflag@corp.com",
                "X-Oidc-Groups": "test-org-viewers",
            },
        )
        account = jit_client.app.state.account_store.get("extflag-corp-com")
        assert account is not None
        assert account.external is True
        assert account.source_provider == "oidc"

    def test_oidc_me_returns_correct_user_id(self, jit_client):
        """The /me endpoint returns the raw OIDC subject as user_id."""
        resp = jit_client.get(
            "/api/v1/auth/me",
            headers={
                "X-Oidc-Subject": "myemail@example.org",
                "X-Oidc-Groups": "test-org-viewers",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["user_id"] == "myemail@example.org"

    def test_oidc_login_with_no_matching_groups(self, jit_client):
        """OIDC login with unrecognised groups still creates shadow account."""
        resp = jit_client.get(
            "/api/v1/auth/me",
            headers={
                "X-Oidc-Subject": "nogroup@corp.com",
                "X-Oidc-Groups": "unknown-group-xyz",
            },
        )
        # Auth should succeed (auth passes; RBAC is for protected resources)
        assert resp.status_code == 200

        # Account is still provisioned
        account = jit_client.app.state.account_store.get("nogroup-corp-com")
        assert account is not None
        assert account.external is True

    def test_oidc_last_login_at_set_on_creation(self, jit_client):
        """last_login_at is set immediately on shadow account creation."""
        jit_client.get(
            "/api/v1/auth/me",
            headers={
                "X-Oidc-Subject": "logintime@corp.com",
                "X-Oidc-Groups": "test-org-viewers",
            },
        )
        account = jit_client.app.state.account_store.get("logintime-corp-com")
        assert account is not None
        assert account.last_login_at != ""
        # Should be a parseable ISO timestamp
        from datetime import datetime

        dt = datetime.fromisoformat(account.last_login_at)
        assert dt.tzinfo is not None
