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
from dfe_engine.auth.groups import GroupStore
from dfe_engine.auth.jit import JitProvisioner


@pytest.fixture
def stores(tmp_path):
    accounts = AccountStore(tmp_path / "accounts")
    groups = GroupStore(tmp_path / "groups")
    groups.create("acme-viewers", roles=["org_viewer"])
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
