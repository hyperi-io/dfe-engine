"""Tests for the local authentication provider (store-backed)."""

import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import bcrypt
import pytest

from dfe_engine.auth import (
    AuthContext,
    AuthenticationError,
    authorize,
)
from dfe_engine.auth.accounts import Account, AccountStore, _dummy_hash, soft_delete_account
from dfe_engine.auth.groups import GroupStore
from dfe_engine.auth.local_provider import LocalAuthProvider
from tests.support.present import present

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

    def test_an_account_bound_to_no_org_signs_in_with_none(self, provider: LocalAuthProvider):
        auth = provider.authenticate("admin", "admin-secret")
        assert auth.org_id == ""

    def test_an_account_signs_in_with_the_org_it_is_bound_to(
        self, provider: LocalAuthProvider, stores: tuple[AccountStore, GroupStore]
    ):
        stores[0].update("operator", org="acme")
        auth = provider.authenticate("operator", "operator-secret")
        assert auth.org_id == "acme"

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


@contextmanager
def _bcrypt_calls() -> Iterator[list[str]]:
    """Name each real ``bcrypt.checkpw`` and ``bcrypt.hashpw`` call made inside the block.

    Read from the profiler, so the functions themselves are not replaced.
    """
    seen: list[str] = []

    def profile(_frame: object, event: str, arg: object) -> None:
        if event == "c_call" and (arg is bcrypt.checkpw or arg is bcrypt.hashpw):
            seen.append(arg.__name__)

    previous = sys.getprofile()
    sys.setprofile(profile)
    try:
        yield seen
    finally:
        sys.setprofile(previous)


def _unknown(_store: AccountStore) -> str:
    return "nobody"


def _active(_store: AccountStore) -> str:
    return "viewer"


def _blocked(store: AccountStore) -> str:
    store.update("viewer", blocked=True)
    return "viewer"


def _disabled(store: AccountStore) -> str:
    store.update("viewer", enabled=False)
    return "viewer"


def _soft_deleted(store: AccountStore) -> str:
    return soft_delete_account(store, "viewer").username


def _external(store: AccountStore) -> str:
    store.update("viewer", external=True)
    return "viewer"


_GENERIC = "Invalid username or password"


class TestAuthenticationFailure:
    @pytest.mark.parametrize(
        ("state", "password", "message"),
        [
            pytest.param(_unknown, "viewer-secret", _GENERIC, id="unknown-user"),
            pytest.param(_active, "wrong-password", _GENERIC, id="active-wrong-password"),
            pytest.param(_blocked, "wrong-password", _GENERIC, id="blocked-wrong-password"),
            pytest.param(_disabled, "wrong-password", _GENERIC, id="disabled-wrong-password"),
            pytest.param(_soft_deleted, "wrong-password", _GENERIC, id="soft-deleted-wrong"),
            pytest.param(_soft_deleted, "viewer-secret", _GENERIC, id="soft-deleted-right"),
            pytest.param(_external, "viewer-secret", _GENERIC, id="external-right-password"),
            pytest.param(_blocked, "viewer-secret", "Account blocked", id="blocked-right"),
            pytest.param(_disabled, "viewer-secret", "Account disabled", id="disabled-right"),
        ],
    )
    def test_account_state_is_reported_only_to_the_right_password(
        self,
        stores: tuple[AccountStore, GroupStore],
        state: Callable[[AccountStore], str],
        password: str,
        message: str,
    ):
        account_store, group_store = stores
        username = state(account_store)
        provider = LocalAuthProvider(account_store, group_store)
        with _bcrypt_calls() as calls, pytest.raises(AuthenticationError) as refused:
            provider.authenticate(username, password)
        assert str(refused.value) == message
        # Exactly one bcrypt check on every path keeps them in one timing class.
        assert calls == ["checkpw"]

    def test_a_cold_process_first_unknown_name_costs_one_check(self, auth_dirs: tuple[Path, Path]):
        """The dummy hash is made with the store, so the first login does no hashing of its own."""
        accounts_dir, groups_dir = auth_dirs
        _dummy_hash.cache_clear()
        provider = LocalAuthProvider(AccountStore(accounts_dir), GroupStore(groups_dir))
        with _bcrypt_calls() as calls, pytest.raises(AuthenticationError, match=_GENERIC):
            provider.authenticate("nobody", "any-password")
        assert calls == ["checkpw"]

    @pytest.mark.parametrize(
        ("change", "password"),
        [
            pytest.param(
                lambda s, u: s.put(present(s.get(u)).model_copy(update={"blocked": True})),
                "viewer-secret",
                id="blocked-verbatim",
            ),
            pytest.param(lambda s, u: s.update(u, enabled=False), "viewer-secret", id="disabled"),
            pytest.param(
                lambda s, u: s.reset_password(u, "changed-secret"),
                "changed-secret",
                id="new-password",
            ),
            pytest.param(lambda s, u: s.delete(u), "viewer-secret", id="deleted"),
        ],
    )
    def test_a_change_landing_mid_login_refuses_it(
        self,
        stores: tuple[AccountStore, GroupStore],
        auth_dirs: tuple[Path, Path],
        change: Callable[[AccountStore, str], object],
        password: str,
    ):
        accounts_dir, _groups_dir = auth_dirs
        racing = _ChangesAfterFirstRead(accounts_dir, change)
        provider = LocalAuthProvider(racing, stores[1])
        with pytest.raises(AuthenticationError, match=_GENERIC):
            provider.authenticate("viewer", password)
        assert racing.changed


class _ChangesAfterFirstRead(AccountStore):
    """A real store where another writer changes the record just after its first read."""

    def __init__(self, accounts_dir: Path, change: Callable[[AccountStore, str], object]) -> None:
        super().__init__(accounts_dir)
        self._change = change
        self.changed = False

    def get(self, username: str) -> Account | None:
        account = super().get(username)
        if account is not None and not self.changed:
            self.changed = True
            self._change(self, username)
        return account


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
        old_hash = present(account_store.get("admin")).password_hash
        account_store.reset_password("admin", "new-password")
        new_hash = present(account_store.get("admin")).password_hash
        assert old_hash != new_hash

        # Verify new password works
        provider = LocalAuthProvider(account_store, group_store)
        auth = provider.authenticate("admin", "new-password")
        assert auth.user_id == "admin"


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
        # has broad argo:* -- test a truly restricted action instead)
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
