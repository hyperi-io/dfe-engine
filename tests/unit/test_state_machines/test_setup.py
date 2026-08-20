#  Project:      dfe-engine
#  File:         tests/unit/test_state_machines/test_setup.py
#  Purpose:      First-run setup state machine
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

from pathlib import Path

import pytest

from dfe_engine.auth.account_durability import AccountGitState
from dfe_engine.auth.accounts import AccountStore
from dfe_engine.auth.oidc.models import OIDCProvider
from dfe_engine.auth.oidc.registry import OIDCProviderRegistry
from dfe_engine.orgs.registry import OrgRegistry
from dfe_engine.state_machines.setup import (
    SETUP_MACHINE,
    STEP_ADMIN_PASSWORD,
    STEP_FIRST_USER,
    STEP_OIDC_PROVIDER,
    STEP_ORGANISATIONS,
    SetupContext,
)


@pytest.fixture
def ctx(tmp_path: Path, monkeypatch) -> SetupContext:
    """A freshly bootstrapped deployment: seeded admin, nothing else configured."""
    monkeypatch.delenv("DFE_AUTH_LOCAL_ADMIN_NAME", raising=False)
    monkeypatch.delenv("DFE_AUTH_LOCAL_ADMIN_PASSWORD", raising=False)
    accounts = AccountStore(tmp_path / "accounts")
    accounts.create("admin", "changeme", groups=["dfe-admins"])
    return SetupContext(
        account_store=accounts,
        org_registry=OrgRegistry(tmp_path / "orgs"),
        oidc_registry=OIDCProviderRegistry(tmp_path / "oidc"),
    )


def test_fresh_deployment_lands_on_the_first_required_step(ctx):
    state = SETUP_MACHINE.evaluate(ctx)

    assert state.complete is False
    assert state.current_step == STEP_ORGANISATIONS
    assert state.pending_steps == [STEP_ORGANISATIONS, STEP_FIRST_USER, STEP_ADMIN_PASSWORD]
    assert state.completed_steps == []


def test_optional_oidc_never_blocks_completion(ctx):
    ctx.org_registry.create("acme")
    ctx.account_store.create("alice", "a-strong-user-password")
    ctx.account_store.reset_password("admin", "a-strong-local-admin-password")

    state = SETUP_MACHINE.evaluate(ctx)

    assert state.complete is True
    assert state.current_step is None
    assert STEP_OIDC_PROVIDER in state.steps
    assert STEP_OIDC_PROVIDER not in state.completed_steps


def test_steps_do_not_depend_on_the_auth_settings_toggles(ctx):
    """The seeded admin logs in whether or not auth.enabled is set.

    ``bootstrap_auth`` seeds it unconditionally and ``POST /auth/login`` never
    consults auth.enabled / auth.local.enabled, so gating these steps on those
    toggles would hide a live default credential. The context does not carry
    them at all — this test pins that.
    """
    state = SETUP_MACHINE.evaluate(ctx)

    assert STEP_FIRST_USER in state.steps
    assert STEP_ADMIN_PASSWORD in state.steps
    assert not hasattr(ctx, "auth_enabled")


def test_admin_password_step_drops_when_there_is_no_break_glass_account(tmp_path):
    no_admin = SetupContext(
        account_store=AccountStore(tmp_path / "accounts"),
        org_registry=OrgRegistry(tmp_path / "orgs"),
    )

    state = SETUP_MACHINE.evaluate(no_admin)

    assert STEP_ADMIN_PASSWORD not in state.steps
    assert STEP_FIRST_USER in state.steps


def test_disabled_oidc_provider_does_not_satisfy_the_step(ctx):
    ctx.oidc_registry.create("entra", OIDCProvider(enabled=False, issuer="https://idp"))

    state = SETUP_MACHINE.evaluate(ctx)

    assert STEP_OIDC_PROVIDER not in state.completed_steps

    ctx.oidc_registry.update("entra", enabled=True)
    assert STEP_OIDC_PROVIDER in SETUP_MACHINE.evaluate(ctx).completed_steps


def test_break_glass_admin_does_not_count_as_the_first_user(ctx):
    assert STEP_FIRST_USER not in SETUP_MACHINE.evaluate(ctx).completed_steps

    ctx.account_store.create("alice", "a-strong-user-password")
    assert STEP_FIRST_USER in SETUP_MACHINE.evaluate(ctx).completed_steps


def test_custom_dfe_admin_name_is_the_break_glass_account(tmp_path, monkeypatch):
    monkeypatch.setenv("DFE_AUTH_LOCAL_ADMIN_NAME", "alt-admin")
    monkeypatch.delenv("DFE_AUTH_LOCAL_ADMIN_PASSWORD", raising=False)
    accounts = AccountStore(tmp_path / "accounts")
    accounts.create("alt-admin", "changeme", groups=["dfe-admins"])
    ctx = SetupContext(
        account_store=accounts,
        org_registry=OrgRegistry(tmp_path / "orgs"),
    )

    state = SETUP_MACHINE.evaluate(ctx)

    assert STEP_FIRST_USER not in state.completed_steps
    assert STEP_ADMIN_PASSWORD in state.steps
    assert STEP_ADMIN_PASSWORD not in state.completed_steps


def test_disabled_account_does_not_count_as_the_first_user(ctx):
    ctx.account_store.create("alice", "a-strong-user-password")
    ctx.account_store.update("alice", enabled=False)

    assert STEP_FIRST_USER not in SETUP_MACHINE.evaluate(ctx).completed_steps


def test_external_account_counts_as_the_first_user(ctx):
    """A JIT-provisioned OIDC identity is a real user — it has no local password."""
    ctx.account_store.create("bob", "", groups=[])
    ctx.account_store.update("bob", external=True, source_provider="entra")

    assert STEP_FIRST_USER in SETUP_MACHINE.evaluate(ctx).completed_steps


def test_admin_password_step_clears_only_after_rotation(ctx):
    assert STEP_ADMIN_PASSWORD not in SETUP_MACHINE.evaluate(ctx).completed_steps

    ctx.account_store.reset_password("admin", "a-strong-local-admin-password")
    assert STEP_ADMIN_PASSWORD in SETUP_MACHINE.evaluate(ctx).completed_steps


def _with_git(ctx: SetupContext, *, merged: bool) -> SetupContext:
    return SetupContext(
        account_store=ctx.account_store,
        org_registry=ctx.org_registry,
        oidc_registry=ctx.oidc_registry,
        break_glass_git=AccountGitState(
            enabled=True, auto_merge=False, committed=True, merged=merged, pending=None
        ),
    )


def test_setup_stays_incomplete_until_break_glass_is_merged(ctx):
    """A rotated password sitting on a review branch is not durable yet."""
    ctx.org_registry.create("acme")
    ctx.account_store.create("alice", "a-strong-user-password")
    ctx.account_store.reset_password("admin", "a-strong-local-admin-password")

    state = SETUP_MACHINE.evaluate(_with_git(ctx, merged=False))

    assert state.complete is False
    assert state.current_step == STEP_ADMIN_PASSWORD
    assert STEP_ADMIN_PASSWORD in state.pending_steps
    assert STEP_ADMIN_PASSWORD not in state.completed_steps


def test_setup_completes_once_break_glass_is_merged(ctx):
    ctx.org_registry.create("acme")
    ctx.account_store.create("alice", "a-strong-user-password")
    ctx.account_store.reset_password("admin", "a-strong-local-admin-password")

    state = SETUP_MACHINE.evaluate(_with_git(ctx, merged=True))

    assert state.complete is True
    assert state.current_step is None
    assert STEP_ADMIN_PASSWORD in state.completed_steps


def test_admin_password_step_uses_env_password_as_rotation_baseline(tmp_path, monkeypatch):
    """The wizard must detect rotation from DFE_AUTH_LOCAL_ADMIN_PASSWORD, not only changeme."""
    monkeypatch.setenv("DFE_AUTH_LOCAL_ADMIN_NAME", "new-admin")
    monkeypatch.setenv("DFE_AUTH_LOCAL_ADMIN_PASSWORD", "test")
    accounts = AccountStore(tmp_path / "accounts")
    accounts.create("new-admin", "test", groups=["dfe-admins"])
    ctx = SetupContext(
        account_store=accounts,
        org_registry=OrgRegistry(tmp_path / "orgs"),
    )

    assert STEP_FIRST_USER not in SETUP_MACHINE.evaluate(ctx).completed_steps
    assert STEP_ADMIN_PASSWORD not in SETUP_MACHINE.evaluate(ctx).completed_steps

    accounts.reset_password("new-admin", "a-strong-local-admin-password")
    assert STEP_ADMIN_PASSWORD in SETUP_MACHINE.evaluate(ctx).completed_steps


def test_only_bootstrapped_stores_contribute_steps(tmp_path):
    state = SETUP_MACHINE.evaluate(SetupContext(org_registry=OrgRegistry(tmp_path / "orgs")))

    assert state.steps == [STEP_ORGANISATIONS]
    assert state.pending_steps == [STEP_ORGANISATIONS]


def test_missing_stores_are_tolerated():
    """The endpoint is reachable while the app is still bootstrapping its stores."""
    state = SETUP_MACHINE.evaluate(SetupContext())

    assert state.steps == []
    assert state.complete is True


def test_status_projects_the_registries_the_wizard_renders(ctx):
    ctx.org_registry.create("acme", display_name="Acme")
    ctx.oidc_registry.create(
        "entra",
        OIDCProvider(enabled=True, issuer="https://idp", client_secret_env="ENTRA_SECRET"),
    )

    status = SETUP_MACHINE.status(ctx)

    assert [o.name for o in status.organisations] == ["acme"]
    assert [p.name for p in status.oidc_providers] == ["entra"]


def test_status_never_carries_accounts(ctx):
    """Whether a user exists is a step verdict; the account list is not exposed."""
    ctx.account_store.create("alice", "a-strong-user-password", groups=["dfe-admins"])

    status = SETUP_MACHINE.status(ctx)

    assert "accounts" not in status.model_dump()
    assert "alice" not in status.model_dump_json()
    assert STEP_FIRST_USER in status.initial_setup.completed_steps


def test_status_withholds_registries_once_setup_is_complete(ctx):
    ctx.org_registry.create("acme")
    ctx.account_store.create("alice", "a-strong-user-password")
    ctx.account_store.reset_password("admin", "a-strong-local-admin-password")

    complete = SETUP_MACHINE.status(ctx)
    assert complete.initial_setup.complete is True
    assert complete.organisations == []
    assert complete.oidc_providers == []

    # ...unless the caller opts in (an authenticated admin view, say).
    unredacted = SETUP_MACHINE.status(ctx, redact_when_complete=False)
    assert [o.name for o in unredacted.organisations] == ["acme"]


def _complete(ctx: SetupContext) -> None:
    ctx.org_registry.create("acme")
    ctx.account_store.create("alice", "a-strong-user-password")
    ctx.account_store.reset_password("admin", "a-strong-local-admin-password")


def test_completed_setup_keeps_oidc_login_options_only(ctx):
    """The login screen still needs the IdPs it can offer, and their labels."""
    ctx.oidc_registry.create(
        "entra",
        OIDCProvider(
            enabled=True,
            display_name="Microsoft Entra",
            issuer="https://idp",
            client_secret_env="ENTRA_SECRET",
        ),
    )
    _complete(ctx)

    status = SETUP_MACHINE.status(ctx)

    assert status.initial_setup.complete is True
    # Name and display name, and nothing else: no issuer, no env vars, no type.
    assert [p.model_dump() for p in status.oidc_providers] == [
        {"name": "entra", "display_name": "Microsoft Entra"},
    ]
    assert "ENTRA_SECRET" not in status.model_dump_json()


def test_completed_setup_leaves_an_unset_display_name_empty(ctx):
    """No invented label — ``name`` is right there for the UI to fall back to."""
    ctx.oidc_registry.create("entra", OIDCProvider(enabled=True, issuer="https://idp"))
    _complete(ctx)

    assert [p.model_dump() for p in SETUP_MACHINE.status(ctx).oidc_providers] == [
        {"name": "entra", "display_name": ""},
    ]


def test_completed_setup_drops_disabled_oidc_providers(ctx):
    """A disabled provider cannot be logged in with, so it is pure inventory."""
    ctx.oidc_registry.create("entra", OIDCProvider(enabled=True, issuer="https://idp"))
    ctx.oidc_registry.create("okta", OIDCProvider(enabled=False, issuer="https://okta"))
    _complete(ctx)

    assert [p.name for p in SETUP_MACHINE.status(ctx).oidc_providers] == ["entra"]


def test_status_surfaces_the_break_glass_git_state(ctx):
    # A pending review PR (production+team): the wizard must see it is not durable yet.
    ctx = SetupContext(
        account_store=ctx.account_store,
        org_registry=ctx.org_registry,
        oidc_registry=ctx.oidc_registry,
        break_glass_git=AccountGitState(
            enabled=True, auto_merge=False, committed=True, merged=False, pending=None
        ),
    )
    status = SETUP_MACHINE.status(ctx)
    assert status.break_glass is not None
    assert status.break_glass.enabled is True
    assert status.break_glass.merged is False


def test_status_break_glass_absent_when_not_computed(ctx):
    # Default context carries no git state -> the field is simply absent (None).
    assert SETUP_MACHINE.status(ctx).break_glass is None


def test_incomplete_setup_still_returns_full_oidc_providers(ctx):
    """The wizard edits providers, so it gets the whole record — disabled included."""
    ctx.oidc_registry.create("okta", OIDCProvider(enabled=False, issuer="https://okta"))

    providers = SETUP_MACHINE.status(ctx).oidc_providers

    assert [p.name for p in providers] == ["okta"]
    assert providers[0].issuer == "https://okta"
