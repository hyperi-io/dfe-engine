#  Project:      dfe-engine
#  File:         tests/e2e/test_jit_workflow.py
#  Purpose:      E2E tests for JIT provisioning workflow on first OIDC login
#  Language:     Python
#
#  License:      FSL-1.1-ALv2
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
    ServicesSettings,
    SourceSettings,
)


@pytest.fixture
def jit_settings(tmp_path):
    (tmp_path / "sources").mkdir()
    (tmp_path / "services").mkdir()
    (tmp_path / "auth").mkdir()
    return DFESettings(
        config_dir=str(tmp_path),
        source=SourceSettings(sources_dir=str(tmp_path / "sources")),
        services=ServicesSettings(config_yaml_dir=str(tmp_path / "services")),
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
