#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_jit.py
#  Purpose:      Unit tests for JitProvisioner shadow account creation
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

import pytest

from dfe_engine.auth.accounts import AccountStore
from dfe_engine.auth.groups import GroupStore
from dfe_engine.auth.jit import JitProvisioner


@pytest.fixture
def stores(tmp_path):
    accounts = AccountStore(tmp_path / "accounts")
    groups = GroupStore(tmp_path / "groups")
    groups.create("acme-viewers", roles=["customer_viewer"])
    groups.update("acme-viewers", org_ids=["acme"])
    groups.create("dfe-admins", roles=["admin"])
    groups.create("dfe-analysts", roles=["data_analyst"])
    return accounts, groups


class TestSanitiseUsername:
    def test_email(self):
        assert JitProvisioner.sanitise_username("jane@corp.com") == "jane-corp-com"

    def test_complex_email(self):
        assert (
            JitProvisioner.sanitise_username("user.name+tag@example.co.uk")
            == "user-name-tag-example-co-uk"
        )

    def test_already_safe(self):
        assert JitProvisioner.sanitise_username("simple-user") == "simple-user"


class TestEnsureAccount:
    def test_first_login_creates_account(self, stores):
        accounts, groups = stores
        jit = JitProvisioner(account_store=accounts, group_store=groups)
        account = jit.ensure_account("jane@corp.com", ["acme-viewers"], "entra")
        assert account is not None
        assert account.external is True
        assert account.source_provider == "entra"
        # Shadow accounts carry an unusable-password sentinel (not a bcrypt hash),
        # so an external identity can never authenticate via local login.
        assert not account.password_hash.startswith("$2")
        assert account.last_login_at != ""

    def test_subsequent_login_updates_timestamp(self, stores):
        accounts, groups = stores
        jit = JitProvisioner(account_store=accounts, group_store=groups)
        jit.ensure_account("jane@corp.com", ["acme-viewers"], "entra")
        first = accounts.get("jane-corp-com")
        jit.ensure_account("jane@corp.com", ["acme-viewers"], "entra")
        second = accounts.get("jane-corp-com")
        assert second.last_login_at >= first.last_login_at

    def test_groups_updated_on_change(self, stores):
        accounts, groups = stores
        jit = JitProvisioner(account_store=accounts, group_store=groups)
        jit.ensure_account("jane@corp.com", ["acme-viewers"], "entra")
        jit.ensure_account("jane@corp.com", ["acme-viewers", "dfe-admins"], "entra")
        account = accounts.get("jane-corp-com")
        assert "dfe-admins" in account.groups

    def test_race_condition_handled(self, stores):
        accounts, groups = stores
        jit = JitProvisioner(account_store=accounts, group_store=groups)
        # Pre-create the account (simulating race)
        accounts.create("race-user-com", "", groups=["acme-viewers"])
        # Should not raise
        account = jit.ensure_account("race@user.com", ["acme-viewers"], "entra")
        assert account is not None


class TestEnsureAccountHdxInvite:
    def test_first_login_no_hdx_client_does_not_crash(self, stores):
        """ensure_account with no HyperDX client should succeed without error."""
        accounts, groups = stores
        jit = JitProvisioner(account_store=accounts, group_store=groups, hyperdx_client=None)
        account = jit.ensure_account("jane@corp.com", ["acme-viewers"], "entra")
        assert account is not None

    def test_first_login_hdx_client_no_org_registry_does_not_crash(self, stores):
        """invite path skips gracefully when org_registry is None."""
        accounts, groups = stores

        class _FakeHdx:
            _connected = False

        jit = JitProvisioner(
            account_store=accounts,
            group_store=groups,
            hyperdx_client=_FakeHdx(),
            org_registry=None,
        )
        # Should not raise even with a fake (disconnected) hdx client
        account = jit.ensure_account("jane@corp.com", ["acme-viewers"], "entra")
        assert account is not None


class TestResolveTeamApiKey:
    def test_returns_empty_without_org_registry(self, stores):
        _, groups = stores
        jit = JitProvisioner(account_store=None, group_store=groups, org_registry=None)
        assert jit._resolve_team_api_key("customer-acme") == ""

    def test_returns_empty_for_broad_team(self, stores):
        _, groups = stores
        jit = JitProvisioner(account_store=None, group_store=groups, org_registry=None)
        assert jit._resolve_team_api_key("dfe-admin") == ""

    def test_returns_empty_when_env_var_unset(self, stores, tmp_path):
        accounts, groups = stores
        from dfe_engine.orgs.registry import OrgRegistry

        org_registry = OrgRegistry(tmp_path / "orgs")
        org_registry.create("acme", org_ids=["acme"])
        org_registry.update("acme", hyperdx_team_api_key_env="HYPERDX_TEAM_API_KEY_ACME")

        jit = JitProvisioner(account_store=accounts, group_store=groups, org_registry=org_registry)
        # Env var not set → empty string
        result = jit._resolve_team_api_key("customer-acme")
        assert result == ""

    def test_returns_key_when_env_var_set(self, stores, tmp_path, monkeypatch):
        accounts, groups = stores
        from dfe_engine.orgs.registry import OrgRegistry

        monkeypatch.setenv("HYPERDX_TEAM_API_KEY_ACME", "team-secret-key")
        org_registry = OrgRegistry(tmp_path / "orgs")
        org_registry.create("acme", org_ids=["acme"])
        org_registry.update("acme", hyperdx_team_api_key_env="HYPERDX_TEAM_API_KEY_ACME")

        jit = JitProvisioner(account_store=accounts, group_store=groups, org_registry=org_registry)
        result = jit._resolve_team_api_key("customer-acme")
        assert result == "team-secret-key"


class TestResolveHyperdxTeam:
    def test_admin_wins(self, stores):
        _, groups = stores
        jit = JitProvisioner(account_store=None, group_store=groups)
        assert jit.resolve_hyperdx_team(["acme-viewers", "dfe-admins"]) == "dfe-admin"

    def test_analyst_wins_over_org(self, stores):
        _, groups = stores
        jit = JitProvisioner(account_store=None, group_store=groups)
        assert jit.resolve_hyperdx_team(["acme-viewers", "dfe-analysts"]) == "dfe-analysts"

    def test_org_scoped_fallback(self, stores):
        _, groups = stores
        jit = JitProvisioner(account_store=None, group_store=groups)
        assert jit.resolve_hyperdx_team(["acme-viewers"]) == "customer-acme"

    def test_no_groups_returns_empty(self, stores):
        _, groups = stores
        jit = JitProvisioner(account_store=None, group_store=groups)
        assert jit.resolve_hyperdx_team([]) == ""

    def test_unknown_group_returns_empty(self, stores):
        _, groups = stores
        jit = JitProvisioner(account_store=None, group_store=groups)
        assert jit.resolve_hyperdx_team(["nonexistent-group"]) == ""
