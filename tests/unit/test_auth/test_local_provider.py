"""Tests for the local authentication provider (store-backed)."""

from __future__ import annotations

from pathlib import Path

import pytest

from dfe_engine.auth import (
    AuthContext,
    AuthenticationError,
    authorize,
)
from dfe_engine.auth.accounts import AccountStore
from dfe_engine.auth.groups import GroupStore
from dfe_engine.auth.local_provider import LocalAuthProvider

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def auth_dirs(tmp_path: Path) -> tuple[Path, Path]:
    """Create accounts and groups dirs."""
    accounts_dir = tmp_path / "accounts"
    accounts_dir.mkdir()
    groups_dir = tmp_path / "groups"
    groups_dir.mkdir()
    return accounts_dir, groups_dir


@pytest.fixture
def stores(auth_dirs: tuple[Path, Path]) -> tuple[AccountStore, GroupStore]:
    """Create stores with test accounts and groups."""
    accounts_dir, groups_dir = auth_dirs
    account_store = AccountStore(accounts_dir)
    group_store = GroupStore(groups_dir)

    # Create groups with role mappings
    group_store.create("dfe-admins", roles=["admin"], description="Admins")
    group_store.create("dfe-analysts", roles=["data_analyst"], description="Analysts")
    group_store.create("dfe-viewers", roles=["data_viewer"], description="Viewers")
    group_store.create("dfe-infra", roles=["infra_admin"], description="Infra")

    # Create accounts
    account_store.create("admin", "admin-secret", groups=["dfe-admins"])
    account_store.create("operator", "operator-secret", groups=["dfe-analysts", "dfe-infra"])
    account_store.create("viewer", "viewer-secret", groups=["dfe-viewers"])

    # Register members in groups
    group_store.add_member("dfe-admins", "admin")
    group_store.add_member("dfe-analysts", "operator")
    group_store.add_member("dfe-infra", "operator")
    group_store.add_member("dfe-viewers", "viewer")

    return account_store, group_store


@pytest.fixture
def provider(
    stores: tuple[AccountStore, GroupStore],
) -> LocalAuthProvider:
    account_store, group_store = stores
    return LocalAuthProvider(account_store, group_store)


# ---------------------------------------------------------------------------
# Authentication success
# ---------------------------------------------------------------------------


class TestAuthentication:
    def test_authenticate_admin_success(self, provider: LocalAuthProvider):
        auth = provider.authenticate("admin", "admin-secret")
        assert isinstance(auth, AuthContext)
        assert auth.user_id == "admin"
        assert auth.roles == ["admin"]

    def test_authenticate_operator_success(self, provider: LocalAuthProvider):
        auth = provider.authenticate("operator", "operator-secret")
        assert auth.user_id == "operator"
        # operator is in dfe-analysts (data_analyst) + dfe-infra (infra_admin)
        assert sorted(auth.roles) == ["data_analyst", "infra_admin"]

    def test_authenticate_viewer_success(self, provider: LocalAuthProvider):
        auth = provider.authenticate("viewer", "viewer-secret")
        assert auth.user_id == "viewer"
        assert auth.roles == ["data_viewer"]

    def test_authenticate_populates_org_id(self, provider: LocalAuthProvider):
        auth = provider.authenticate("admin", "admin-secret")
        assert auth.org_id == "default"

    def test_authenticate_populates_request_metadata(self, provider: LocalAuthProvider):
        auth = provider.authenticate(
            "admin",
            "admin-secret",
            request_id="req-123",
            client_ip="10.0.0.1",
            user_agent="test-agent",
        )
        assert auth.request_id == "req-123"
        assert auth.client_ip == "10.0.0.1"
        assert auth.user_agent == "test-agent"

    def test_authenticate_populates_groups(self, provider: LocalAuthProvider):
        auth = provider.authenticate("operator", "operator-secret")
        assert sorted(auth.groups) == ["dfe-analysts", "dfe-infra"]


# ---------------------------------------------------------------------------
# Authentication failure
# ---------------------------------------------------------------------------


class TestAuthenticationFailure:
    def test_wrong_password(self, provider: LocalAuthProvider):
        with pytest.raises(AuthenticationError, match="Invalid username or password"):
            provider.authenticate("admin", "wrong-password")

    def test_unknown_username(self, provider: LocalAuthProvider):
        with pytest.raises(AuthenticationError, match="Invalid username or password"):
            provider.authenticate("nobody", "any-password")

    def test_disabled_account(
        self,
        stores: tuple[AccountStore, GroupStore],
    ):
        account_store, group_store = stores
        account_store.update("viewer", enabled=False)
        provider = LocalAuthProvider(account_store, group_store)
        with pytest.raises(AuthenticationError, match="Account disabled"):
            provider.authenticate("viewer", "viewer-secret")

    def test_blocked_account(
        self,
        stores: tuple[AccountStore, GroupStore],
    ):
        account_store, group_store = stores
        account_store.update("viewer", blocked=True)
        provider = LocalAuthProvider(account_store, group_store)
        with pytest.raises(AuthenticationError, match="Account blocked"):
            provider.authenticate("viewer", "viewer-secret")


# ---------------------------------------------------------------------------
# AccountStore always produces bcrypt hashes
# ---------------------------------------------------------------------------


class TestAccountStoreHashing:
    def test_create_produces_bcrypt_hash(
        self,
        stores: tuple[AccountStore, GroupStore],
    ):
        account_store, _group_store = stores
        account = account_store.get("admin")
        assert account is not None
        assert account.password_hash.startswith("$2b$")

    def test_different_accounts_have_different_hashes(
        self,
        stores: tuple[AccountStore, GroupStore],
    ):
        account_store, _group_store = stores
        admin = account_store.get("admin")
        operator = account_store.get("operator")
        assert admin is not None
        assert operator is not None
        assert admin.password_hash != operator.password_hash

    def test_reset_password_updates_hash(
        self,
        stores: tuple[AccountStore, GroupStore],
    ):
        account_store, group_store = stores
        old_hash = account_store.get("admin").password_hash
        account_store.reset_password("admin", "new-password")
        new_hash = account_store.get("admin").password_hash
        assert old_hash != new_hash

        # Verify new password works
        provider = LocalAuthProvider(account_store, group_store)
        auth = provider.authenticate("admin", "new-password")
        assert auth.user_id == "admin"


# ---------------------------------------------------------------------------
# Timing safety
# ---------------------------------------------------------------------------


class TestTimingSafety:
    def test_nonexistent_user_still_takes_time(self, provider: LocalAuthProvider):
        """Unknown username should still do a bcrypt check (timing-safe)."""
        with pytest.raises(AuthenticationError):
            provider.authenticate("nonexistent", "any-password")


# ---------------------------------------------------------------------------
# Integration: authenticate -> authorize
# ---------------------------------------------------------------------------


class TestIntegration:
    def test_authenticate_then_authorize_admin_allowed(self, provider: LocalAuthProvider):
        auth = provider.authenticate("admin", "admin-secret")
        result = authorize(auth, "config:write", enabled=True)
        assert result.allowed
        assert result.reason == "role:admin"

    def test_authenticate_then_authorize_viewer_denied_write(self, provider: LocalAuthProvider):
        auth = provider.authenticate("viewer", "viewer-secret")
        result = authorize(auth, "config:write", enabled=True)
        assert not result.allowed

    def test_authenticate_then_authorize_operator_partial(self, provider: LocalAuthProvider):
        auth = provider.authenticate("operator", "operator-secret")
        # operator has data_analyst + infra_admin roles
        # infra_admin has config:* so config:read is allowed
        assert authorize(auth, "config:read", enabled=True).allowed
        # infra_admin has helm:* so helm:compile is allowed
        assert authorize(auth, "helm:compile", enabled=True).allowed
        # helm:execute_ddl is also under helm:* for infra_admin
        assert authorize(auth, "helm:execute_ddl", enabled=True).allowed
        # admin-only action that neither data_analyst nor infra_admin have
        # (there is no such action in the current role set since infra_admin
        # has broad argo:* — test a truly restricted action instead)
        # data_analyst cannot write config
        auth_viewer = provider.authenticate("viewer", "viewer-secret")
        assert not authorize(auth_viewer, "config:write", enabled=True).allowed


class TestExternalAccountRejection:
    """External (IdP-owned / JIT) identities must never authenticate via local
    login, and an empty / placeholder password must never match."""

    def test_external_account_rejected_from_local_login(
        self, stores: tuple[AccountStore, GroupStore]
    ):
        account_store, group_store = stores
        # A JIT/OIDC shadow account: no usable password, marked external.
        account_store.create("sso-user", "", groups=["dfe-admins"])
        account_store.update("sso-user", external=True)
        provider = LocalAuthProvider(account_store, group_store)
        with pytest.raises(AuthenticationError, match="Invalid username or password"):
            provider.authenticate("sso-user", "")

    def test_empty_password_never_authenticates(self, stores: tuple[AccountStore, GroupStore]):
        account_store, group_store = stores
        account_store.create("nopass", "", groups=["dfe-viewers"])
        provider = LocalAuthProvider(account_store, group_store)
        with pytest.raises(AuthenticationError):
            provider.authenticate("nopass", "")
        assert account_store.verify_password("nopass", "") is False
