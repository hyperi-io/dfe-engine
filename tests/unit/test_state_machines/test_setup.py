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
def ctx(tmp_path: Path) -> SetupContext:
    """A freshly bootstrapped deployment: seeded admin, nothing else configured."""
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
