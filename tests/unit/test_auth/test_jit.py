#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_jit.py
#  Purpose:      Unit tests for JitProvisioner shadow account creation
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

import asyncio
from unittest.mock import patch

import pytest

from dfe_engine.auth.accounts import AccountStore
from dfe_engine.auth.breakglass import USERNAME as BREAKGLASS_USERNAME
from dfe_engine.auth.groups import GroupStore
from dfe_engine.auth.jit import JitIdentityCollisionError, JitProvisioner
from dfe_engine.auth.scim_mapping import SCIM_SOURCE_PROVIDER


@pytest.fixture
def stores(tmp_path):
    accounts = AccountStore(tmp_path / "accounts")
    groups = GroupStore(tmp_path / "groups")
    groups.create("acme-viewers", roles=["org_viewer"])
    groups.update("acme-viewers", org_ids=["acme"])
    groups.create("dfe-admins", roles=["admin"])
    groups.create("dfe-analysts", roles=["data_analyst"])
    return accounts, groups


class RacingAccountStore(AccountStore):
    """A real store whose first ``get`` of one username misses.

    What a request sees when another creates the account between its own lookup
    and its create -- the only way into ``ensure_account``'s ValueError branch.
    """

    def __init__(self, accounts_dir, *, blind_to: str) -> None:
        super().__init__(accounts_dir)
        self._blind_to = blind_to

    def get(self, username):
        if username == self._blind_to:
            self._blind_to = ""
            return None
        return super().get(username)


def external_account(store: AccountStore, username: str, provider: str, groups: list[str]):
    """Seed a shadow account owned by *provider*, as a first JIT login leaves it."""
    store.create(username, "", groups=groups)
    return store.update(username, external=True, source_provider=provider)


def scim_account(store, username: str):
    """Seed an account as the SCIM face provisions one (``api/v1/scim.py`` create_user).

    The stamp comes from the mapper the route uses, so the test follows a change
    of stamp rather than pinning a copy of it.
    """
    store.create(username, "provisioned-Pw-1", groups=[])
    return store.update(username, enabled=True, source_provider=SCIM_SOURCE_PROVIDER)


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

    def test_first_login_stores_oidc_email(self, stores):
        accounts, groups = stores
        jit = JitProvisioner(account_store=accounts, group_store=groups)
        account = jit.ensure_account("guid-123", ["acme-viewers"], "entra", email="jane@corp.com")
        assert account.email == "jane@corp.com"

    def test_subsequent_login_backfills_oidc_email(self, stores):
        accounts, groups = stores
        jit = JitProvisioner(account_store=accounts, group_store=groups)
        jit.ensure_account("guid-123", ["acme-viewers"], "entra")
        assert accounts.get("guid-123").email == ""

        jit.ensure_account("guid-123", ["acme-viewers"], "entra", email="jane@corp.com")
        assert accounts.get("guid-123").email == "jane@corp.com"

    def test_first_login_stores_oidc_name(self, stores):
        accounts, groups = stores
        jit = JitProvisioner(account_store=accounts, group_store=groups)
        account = jit.ensure_account(
            name="Jane Citizen",
            oidc_groups=["acme-viewers"],
            source_provider="okta",
            user_id="00u15mxs3ecygt7oj698",
        )
        assert account.name == "Jane Citizen"

    def test_subsequent_login_backfills_oidc_name(self, stores):
        accounts, groups = stores
        jit = JitProvisioner(account_store=accounts, group_store=groups)
        jit.ensure_account(
            oidc_groups=["acme-viewers"], source_provider="okta", user_id="00u15mxs3ecygt7oj698"
        )
        assert accounts.get("00u15mxs3ecygt7oj698").name == ""

        jit.ensure_account(
            name="Jane Citizen",
            oidc_groups=["acme-viewers"],
            source_provider="okta",
            user_id="00u15mxs3ecygt7oj698",
        )
        assert accounts.get("00u15mxs3ecygt7oj698").name == "Jane Citizen"

    def test_login_without_name_keeps_stored_name(self, stores):
        accounts, groups = stores
        jit = JitProvisioner(account_store=accounts, group_store=groups)
        jit.ensure_account(
            name="Jane Citizen",
            oidc_groups=["acme-viewers"],
            source_provider="okta",
            user_id="00u15mxs3ecygt7oj698",
        )
        jit.ensure_account(
            oidc_groups=["acme-viewers"], source_provider="okta", user_id="00u15mxs3ecygt7oj698"
        )
        assert accounts.get("00u15mxs3ecygt7oj698").name == "Jane Citizen"

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

    def test_race_condition_handled(self, tmp_path, stores):
        _, groups = stores
        accounts = RacingAccountStore(tmp_path / "accounts", blind_to="race-user-com")
        external_account(accounts, "race-user-com", "entra", ["acme-viewers"])
        jit = JitProvisioner(account_store=accounts, group_store=groups)

        # The lookup misses, the create loses the race, the ValueError branch runs.
        account = jit.ensure_account("race@user.com", ["acme-viewers", "dfe-admins"], "entra")

        assert account is not None
        assert account.groups == ["acme-viewers", "dfe-admins"]


class TestCrossIdentityRefusal:
    """An IdP must not reach an account its own identity does not own (#419).

    The account key is a sanitised subject with no provider in it, so a provider
    asserting ``sub: admin`` once landed on the local admin and replaced its
    group list -- privilege assignment by a third party.
    """

    def test_a_local_account_is_refused_and_left_untouched(self, stores):
        accounts, groups = stores
        accounts.create("jane-corp-com", "localpass", groups=["acme-viewers"], email="j@dfe.local")
        jit = JitProvisioner(account_store=accounts, group_store=groups)

        with pytest.raises(JitIdentityCollisionError) as refused:
            jit.ensure_account("jane@corp.com", ["dfe-admins"], "entra", email="evil@example.com")

        assert refused.value.reason == "local_account"
        stored = accounts.get("jane-corp-com")
        assert stored.groups == ["acme-viewers"]
        assert stored.email == "j@dfe.local"
        assert stored.last_login_at == ""
        assert stored.external is False

    def test_the_race_branch_refuses_a_local_account(self, tmp_path, stores):
        _, groups = stores
        accounts = RacingAccountStore(tmp_path / "accounts", blind_to="jane-corp-com")
        accounts.create("jane-corp-com", "localpass", groups=["acme-viewers"], email="j@dfe.local")
        jit = JitProvisioner(account_store=accounts, group_store=groups)

        with pytest.raises(JitIdentityCollisionError) as refused:
            jit.ensure_account("jane@corp.com", ["dfe-admins"], "entra", email="evil@example.com")

        assert refused.value.reason == "local_account"
        stored = AccountStore(tmp_path / "accounts").get("jane-corp-com")
        assert stored.groups == ["acme-viewers"]
        assert stored.email == "j@dfe.local"

    def test_another_providers_account_is_refused(self, stores):
        accounts, groups = stores
        external_account(accounts, "jane-corp-com", "okta", ["acme-viewers"])
        jit = JitProvisioner(account_store=accounts, group_store=groups)

        with pytest.raises(JitIdentityCollisionError) as refused:
            jit.ensure_account("jane@corp.com", ["dfe-admins"], "entra")

        assert refused.value.reason == "provider_mismatch"
        assert accounts.get("jane-corp-com").groups == ["acme-viewers"]

    def test_the_race_branch_refuses_another_provider(self, tmp_path, stores):
        _, groups = stores
        accounts = RacingAccountStore(tmp_path / "accounts", blind_to="jane-corp-com")
        external_account(accounts, "jane-corp-com", "okta", ["acme-viewers"])
        jit = JitProvisioner(account_store=accounts, group_store=groups)

        with pytest.raises(JitIdentityCollisionError) as refused:
            jit.ensure_account("jane@corp.com", ["dfe-admins"], "entra")

        assert refused.value.reason == "provider_mismatch"
        assert AccountStore(tmp_path / "accounts").get("jane-corp-com").groups == ["acme-viewers"]

    def test_the_owning_provider_still_updates(self, stores):
        """The negative control: an ordinary shadow account still reconciles."""
        accounts, groups = stores
        external_account(accounts, "jane-corp-com", "entra", ["acme-viewers"])
        jit = JitProvisioner(account_store=accounts, group_store=groups)

        account = jit.ensure_account(
            "jane@corp.com", ["acme-viewers", "dfe-admins"], "entra", email="jane@corp.com"
        )

        assert account.groups == ["acme-viewers", "dfe-admins"]
        assert account.email == "jane@corp.com"
        assert account.last_login_at != ""

    def test_the_message_names_no_account(self, stores):
        """The client is told it was refused, never which local names are taken."""
        accounts, groups = stores
        accounts.create("jane-corp-com", "localpass", groups=["acme-viewers"])
        jit = JitProvisioner(account_store=accounts, group_store=groups)

        with pytest.raises(JitIdentityCollisionError) as refused:
            jit.ensure_account("jane@corp.com", ["dfe-admins"], "entra")

        message = str(refused.value)
        assert message == "OIDC login refused"
        assert "jane" not in message
        assert refused.value.reason not in message

    def test_the_refusal_is_audited(self, stores):
        """A silent refusal is a security event nobody sees."""
        accounts, groups = stores
        accounts.create("jane-corp-com", "localpass", groups=["acme-viewers"])
        jit = JitProvisioner(account_store=accounts, group_store=groups)

        with patch("dfe_engine.auth.jit.audit_jit_login_refused") as audited:
            with pytest.raises(JitIdentityCollisionError):
                jit.ensure_account("jane@corp.com", ["dfe-admins"], "entra")

        audited.assert_called_once_with("jane@corp.com", "entra", "local_account")


class TestSourceProviderBinding:
    """SCIM and OIDC from the same IdP are ONE identity source (#506).

    A SCIM-provisioned user is stamped ``scim`` and carries ``external=False``, so
    the identity guard read their first OIDC login as a collision with a local
    account. ``auth.source_provider_bindings`` is how a deployment declares that
    its SCIM connector and one named OIDC provider are the same IdP.
    """

    def test_an_unbound_scim_account_is_refused(self, stores):
        """Fail closed: no binding, no adoption."""
        accounts, groups = stores
        scim_account(accounts, "jane-corp-com")
        jit = JitProvisioner(account_store=accounts, group_store=groups)

        with pytest.raises(JitIdentityCollisionError) as refused:
            jit.ensure_account("jane@corp.com", ["dfe-admins"], "entra")

        assert refused.value.reason == "provider_mismatch"
        assert accounts.get("jane-corp-com").groups == []

    def test_the_bound_provider_reconciles_the_account(self, stores):
        accounts, groups = stores
        scim_account(accounts, "jane-corp-com")
        jit = JitProvisioner(
            account_store=accounts,
            group_store=groups,
            source_provider_bindings={SCIM_SOURCE_PROVIDER: "entra"},
        )

        account = jit.ensure_account(
            "jane@corp.com", ["acme-viewers", "dfe-admins"], "entra", email="jane@corp.com"
        )

        assert account.groups == ["acme-viewers", "dfe-admins"]
        assert account.email == "jane@corp.com"
        assert account.last_login_at != ""

    def test_adoption_leaves_the_scim_stamp_alone(self, stores):
        """SCIM still owns the record, so removing the binding re-closes the door."""
        accounts, groups = stores
        scim_account(accounts, "jane-corp-com")
        jit = JitProvisioner(
            account_store=accounts,
            group_store=groups,
            source_provider_bindings={SCIM_SOURCE_PROVIDER: "entra"},
        )

        account = jit.ensure_account("jane@corp.com", ["dfe-admins"], "entra")

        assert account.source_provider == SCIM_SOURCE_PROVIDER
        assert account.external is False

    def test_an_unbound_provider_is_still_refused(self, stores):
        """The binding names ONE provider -- account_write is all it takes to mint a
        SCIM account, so an open rule would let any IdP claim another's users."""
        accounts, groups = stores
        scim_account(accounts, "jane-corp-com")
        jit = JitProvisioner(
            account_store=accounts,
            group_store=groups,
            source_provider_bindings={SCIM_SOURCE_PROVIDER: "entra"},
        )

        with pytest.raises(JitIdentityCollisionError) as refused:
            jit.ensure_account("jane@corp.com", ["dfe-admins"], "okta")

        assert refused.value.reason == "provider_mismatch"
        assert accounts.get("jane-corp-com").groups == []

    def test_the_race_branch_honours_the_binding(self, tmp_path, stores):
        """Both write paths, not just the main one."""
        _, groups = stores
        accounts = RacingAccountStore(tmp_path / "accounts", blind_to="jane-corp-com")
        scim_account(accounts, "jane-corp-com")
        jit = JitProvisioner(
            account_store=accounts,
            group_store=groups,
            source_provider_bindings={SCIM_SOURCE_PROVIDER: "entra"},
        )

        account = jit.ensure_account("jane@corp.com", ["dfe-admins"], "entra")

        assert account.groups == ["dfe-admins"]

    def test_the_race_branch_refuses_an_unbound_provider(self, tmp_path, stores):
        _, groups = stores
        accounts = RacingAccountStore(tmp_path / "accounts", blind_to="jane-corp-com")
        scim_account(accounts, "jane-corp-com")
        jit = JitProvisioner(
            account_store=accounts,
            group_store=groups,
            source_provider_bindings={SCIM_SOURCE_PROVIDER: "entra"},
        )

        with pytest.raises(JitIdentityCollisionError) as refused:
            jit.ensure_account("jane@corp.com", ["dfe-admins"], "okta")

        assert refused.value.reason == "provider_mismatch"
        assert AccountStore(tmp_path / "accounts").get("jane-corp-com").groups == []

    @pytest.mark.parametrize(
        "bindings",
        [
            {SCIM_SOURCE_PROVIDER: "entra"},
            # The empty stamp is what a local account carries, so an operator who
            # writes it must still not hand any IdP the local credentials.
            {"": "entra"},
            {"oidc": "entra", "": "entra", SCIM_SOURCE_PROVIDER: "entra"},
        ],
    )
    def test_a_local_account_stays_unclaimable(self, stores, bindings):
        """DFE runs standalone on local accounts, so no binding widens the local rule."""
        accounts, groups = stores
        accounts.create("jane-corp-com", "localpass", groups=["acme-viewers"], email="j@dfe.local")
        jit = JitProvisioner(
            account_store=accounts, group_store=groups, source_provider_bindings=bindings
        )

        with pytest.raises(JitIdentityCollisionError) as refused:
            jit.ensure_account("jane@corp.com", ["dfe-admins"], "entra", email="evil@example.com")

        assert refused.value.reason == "local_account"
        stored = accounts.get("jane-corp-com")
        assert stored.groups == ["acme-viewers"]
        assert stored.email == "j@dfe.local"
        assert stored.external is False
        assert stored.last_login_at == ""

    def test_the_race_branch_refuses_a_local_account_with_bindings_set(self, tmp_path, stores):
        _, groups = stores
        accounts = RacingAccountStore(tmp_path / "accounts", blind_to="jane-corp-com")
        accounts.create("jane-corp-com", "localpass", groups=["acme-viewers"], email="j@dfe.local")
        jit = JitProvisioner(
            account_store=accounts,
            group_store=groups,
            source_provider_bindings={"": "entra", SCIM_SOURCE_PROVIDER: "entra"},
        )

        with pytest.raises(JitIdentityCollisionError) as refused:
            jit.ensure_account("jane@corp.com", ["dfe-admins"], "entra")

        assert refused.value.reason == "local_account"
        assert AccountStore(tmp_path / "accounts").get("jane-corp-com").email == "j@dfe.local"

    def test_a_caller_asserting_no_provider_adopts_nothing(self, stores):
        """A misconfigured proxy_provider must not become a wildcard."""
        accounts, groups = stores
        external_account(accounts, "jane-corp-com", "entra", ["acme-viewers"])
        jit = JitProvisioner(
            account_store=accounts, group_store=groups, source_provider_bindings={"entra": ""}
        )

        with pytest.raises(JitIdentityCollisionError) as refused:
            jit.ensure_account("jane@corp.com", ["dfe-admins"], "")

        assert refused.value.reason == "provider_mismatch"
        assert accounts.get("jane-corp-com").groups == ["acme-viewers"]

    @pytest.mark.parametrize("protected", ["admin", BREAKGLASS_USERNAME])
    def test_a_binding_cannot_reach_a_recovery_credential(self, stores, protected):
        """The protected floor runs before the identity guard, binding or not."""
        accounts, groups = stores
        scim_account(accounts, protected)
        jit = JitProvisioner(
            account_store=accounts,
            group_store=groups,
            source_provider_bindings={SCIM_SOURCE_PROVIDER: "entra"},
        )

        with pytest.raises(JitIdentityCollisionError) as refused:
            jit.ensure_account(protected, ["dfe-admins"], "entra")

        assert refused.value.reason == "protected_account"
        assert accounts.get(protected).groups == []

    def test_the_refusal_is_audited(self, stores):
        accounts, groups = stores
        scim_account(accounts, "jane-corp-com")
        jit = JitProvisioner(account_store=accounts, group_store=groups)

        with patch("dfe_engine.auth.jit.audit_jit_login_refused") as audited:
            with pytest.raises(JitIdentityCollisionError):
                jit.ensure_account("jane@corp.com", ["dfe-admins"], "entra")

        audited.assert_called_once_with("jane@corp.com", "entra", "provider_mismatch")


class TestRecoveryCredentialFloor:
    """The admin and break-glass accounts are how an operator gets in when
    federation is broken or hostile, so no IdP assertion reaches either."""

    @pytest.mark.parametrize("protected", ["admin", BREAKGLASS_USERNAME])
    def test_a_seeded_recovery_account_is_refused(self, stores, protected):
        accounts, groups = stores
        accounts.create(protected, "localpass", groups=["dfe-admins"], email="op@dfe.local")
        jit = JitProvisioner(account_store=accounts, group_store=groups)

        with pytest.raises(JitIdentityCollisionError) as refused:
            jit.ensure_account(protected, ["acme-viewers"], "entra", email="evil@example.com")

        assert refused.value.reason == "protected_account"
        stored = accounts.get(protected)
        assert stored.groups == ["dfe-admins"]
        assert stored.email == "op@dfe.local"
        assert stored.external is False
        assert stored.last_login_at == ""

    @pytest.mark.parametrize("protected", ["admin", BREAKGLASS_USERNAME])
    def test_a_recovery_name_is_never_created(self, stores, protected):
        """Refused before the store is read, so the name cannot be squatted either."""
        accounts, groups = stores
        jit = JitProvisioner(account_store=accounts, group_store=groups)

        with patch("dfe_engine.auth.jit.audit_jit_login_refused") as audited:
            with pytest.raises(JitIdentityCollisionError):
                jit.ensure_account(protected, ["dfe-admins"], "entra")

        assert accounts.get(protected) is None
        audited.assert_called_once_with(protected, "entra", "protected_account")

    def test_the_floor_holds_even_when_the_provider_matches(self, stores):
        """Independent of the identity guard: a mis-seeded external admin is still refused."""
        accounts, groups = stores
        external_account(accounts, "admin", "entra", ["dfe-admins"])
        jit = JitProvisioner(account_store=accounts, group_store=groups)

        with pytest.raises(JitIdentityCollisionError) as refused:
            jit.ensure_account("admin", ["acme-viewers"], "entra")

        assert refused.value.reason == "protected_account"
        assert accounts.get("admin").groups == ["dfe-admins"]

    def test_the_floor_follows_a_renamed_admin(self, stores):
        accounts, groups = stores
        accounts.create("operator", "localpass", groups=["dfe-admins"])
        jit = JitProvisioner(account_store=accounts, group_store=groups, admin_name="operator")

        with pytest.raises(JitIdentityCollisionError) as refused:
            jit.ensure_account("operator", ["acme-viewers"], "entra")

        assert refused.value.reason == "protected_account"
        assert accounts.get("operator").groups == ["dfe-admins"]

    @pytest.mark.parametrize("subject", ["ADMIN", "_admin_", ".admin.", "admin!"])
    def test_a_subject_that_sanitises_onto_a_recovery_name_is_refused(self, stores, subject):
        """sanitise_username collapses case and punctuation, so the floor compares the key."""
        accounts, groups = stores
        accounts.create("admin", "localpass", groups=["dfe-admins"])
        jit = JitProvisioner(account_store=accounts, group_store=groups)

        with pytest.raises(JitIdentityCollisionError) as refused:
            jit.ensure_account(subject, ["acme-viewers"], "entra")

        assert refused.value.reason == "protected_account"
        assert accounts.get("admin").groups == ["dfe-admins"]


class TestEnsureAccountHdxInvite:
    def test_first_login_no_hdx_client_does_not_crash(self, stores):
        """ensure_account with no HyperDX client should succeed without error."""
        accounts, groups = stores
        jit = JitProvisioner(account_store=accounts, group_store=groups, hyperdx_client=None)
        account = jit.ensure_account("jane@corp.com", ["acme-viewers"], "entra")
        assert account is not None

    def test_first_login_with_disconnected_hdx_client_does_not_crash(self, stores):
        """invite path is fire-and-forget and never breaks account creation."""
        accounts, groups = stores

        class _FakeHdx:
            _connected = False

        jit = JitProvisioner(
            account_store=accounts,
            group_store=groups,
            hyperdx_client=_FakeHdx(),
        )
        # Should not raise even with a fake (disconnected) hdx client
        account = jit.ensure_account("jane@corp.com", ["acme-viewers"], "entra")
        assert account is not None

    def test_refused_invite_is_logged_and_not_audited(self, stores):
        accounts, groups = stores

        class _RefusingHdx:
            async def invite_member(self, user_id):
                return False

        jit = JitProvisioner(
            account_store=accounts, group_store=groups, hyperdx_client=_RefusingHdx()
        )
        with (
            patch("dfe_engine.auth.jit.audit_jit_hdx_invited") as audited,
            patch("dfe_engine.auth.jit.logger") as log,
        ):
            asyncio.run(jit._invite_to_hdx("jane@corp.com", "customer-acme"))
        audited.assert_not_called()
        log.warning.assert_called_once()
        assert log.warning.call_args.kwargs["user_id"] == "jane@corp.com"

    def test_failed_invite_is_logged_with_the_error_and_not_raised(self, stores):
        accounts, groups = stores

        class _BrokenHdx:
            async def invite_member(self, user_id):
                raise ConnectionError("hyperdx unreachable")

        jit = JitProvisioner(
            account_store=accounts, group_store=groups, hyperdx_client=_BrokenHdx()
        )
        with (
            patch("dfe_engine.auth.jit.audit_jit_hdx_invited") as audited,
            patch("dfe_engine.auth.jit.logger") as log,
        ):
            asyncio.run(jit._invite_to_hdx("jane@corp.com", "customer-acme"))
        audited.assert_not_called()
        log.warning.assert_called_once()
        assert log.warning.call_args.kwargs["error"] == "hyperdx unreachable"


class _RecordingHdx:
    """HyperDX client stub recording the invites it was actually awaited for."""

    def __init__(self) -> None:
        self.invited: list[str] = []

    async def invite_member(self, email: str) -> bool:
        self.invited.append(email)
        return True


class TestHdxInviteScheduling:
    """The invite is the org-scoped user's only route to a HyperDX team.

    Scheduling it onto a loop that never runs drops it in silence, so where it
    is scheduled matters as much as that it is (issue #262).
    """

    async def test_invite_runs_under_a_running_loop(self, stores):
        accounts, groups = stores
        hdx = _RecordingHdx()
        jit = JitProvisioner(account_store=accounts, group_store=groups, hyperdx_client=hdx)

        jit.ensure_account("jane@corp.com", ["acme-viewers"], "entra")

        assert jit._invite_tasks, "invite was not scheduled onto the running loop"
        await asyncio.gather(*jit._invite_tasks)
        assert hdx.invited == ["jane@corp.com"]

    def test_no_running_loop_logs_and_leaves_no_coroutine(self, stores):
        """Off a loop the invite cannot run, so say whose it was."""
        accounts, groups = stores
        jit = JitProvisioner(
            account_store=accounts,
            group_store=groups,
            hyperdx_client=_RecordingHdx(),
        )

        with (
            patch.object(JitProvisioner, "_invite_to_hdx") as never_built,
            patch("dfe_engine.auth.jit.logger") as mock_logger,
        ):
            account = jit.ensure_account("jane@corp.com", ["acme-viewers"], "entra")

        # Not called means the coroutine was never constructed, so none is left
        # un-awaited for the garbage collector to complain about.
        never_built.assert_not_called()
        assert jit._invite_tasks == set()
        assert account is not None

        warning = mock_logger.warning.call_args
        assert warning.kwargs["user_id"] == "jane@corp.com"
        assert warning.kwargs["team_name"] == "customer-acme"


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

    def test_an_entra_group_guid_resolves_through_the_source_id(self, stores):
        """Entra sends object GUIDs, so the team has to resolve the same way roles do."""
        _, groups = stores
        groups.update("dfe-admins", source_id="0295f72c-e3f8-4962-9183-f95ef939e3b8")
        jit = JitProvisioner(account_store=None, group_store=groups)
        assert jit.resolve_hyperdx_team(["0295f72c-e3f8-4962-9183-f95ef939e3b8"]) == "dfe-admin"


class TestOrgIdsFromGroupGuids:
    def test_org_ids_resolve_through_the_source_id(self, stores):
        accounts, groups = stores
        groups.update("acme-viewers", source_id="7b1d0f3e-0000-4000-8000-000000000001")
        jit = JitProvisioner(account_store=accounts, group_store=groups)

        with patch("dfe_engine.auth.jit.audit_jit_account_created") as audit:
            jit.ensure_account("guid-123", ["7b1d0f3e-0000-4000-8000-000000000001"], "entra")

        assert audit.call_args.args[3] == ["acme"]
