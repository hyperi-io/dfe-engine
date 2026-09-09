#  Project:      dfe-engine
#  File:         tests/unit/test_state_machines/test_setup.py
#  Purpose:      First-run setup state machine
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from dfe_engine.auth.account_durability import AccountGitState
from dfe_engine.auth.accounts import AccountStore
from dfe_engine.auth.breakglass import USERNAME as BREAKGLASS_USERNAME
from dfe_engine.auth.oidc.models import OIDCProvider
from dfe_engine.auth.oidc.registry import OIDCProviderRegistry
from dfe_engine.orgs.registry import OrgRegistry
from dfe_engine.state_machines.setup import (
    SETUP_MACHINE,
    STEP_FIRST_USER,
    STEP_OIDC_PROVIDER,
    STEP_ORGANISATIONS,
    SetupContext,
)

# A password a deployment minted, as opposed to the shipped default.
MINTED_ADMIN_PASSWORD = "a-minted-admin-password"


@pytest.fixture
def ctx(tmp_path: Path, monkeypatch) -> SetupContext:
    """A freshly bootstrapped deployment: seeded admin, nothing else configured."""
    accounts = AccountStore(tmp_path / "accounts")
    accounts.create("admin", "changeme", groups=["dfe-admins"])
    return SetupContext(
        account_store=accounts,
        org_registry=OrgRegistry(tmp_path / "orgs"),
        oidc_registry=OIDCProviderRegistry(tmp_path / "oidc"),
    )


def _minted(ctx: SetupContext, **overrides) -> SetupContext:
    """The same deployment, with the admin password minted rather than defaulted."""
    return SetupContext(
        account_store=ctx.account_store,
        org_registry=ctx.org_registry,
        oidc_registry=ctx.oidc_registry,
        bootstrap_admin_password=MINTED_ADMIN_PASSWORD,
        **overrides,
    )


def test_fresh_deployment_lands_on_the_first_required_step(ctx):
    state = SETUP_MACHINE.evaluate(ctx)

    assert state.complete is False
    assert state.current_step == STEP_ORGANISATIONS
    assert state.pending_steps == [STEP_ORGANISATIONS, STEP_FIRST_USER]
    assert state.completed_steps == []


def test_optional_oidc_never_blocks_completion(ctx):
    ctx.org_registry.create("acme")
    ctx.account_store.create("alice", "a-strong-user-password")

    state = SETUP_MACHINE.evaluate(_minted(ctx))

    assert state.complete is True
    assert state.current_step is None
    assert STEP_OIDC_PROVIDER in state.steps
    assert STEP_OIDC_PROVIDER not in state.completed_steps


def test_steps_do_not_depend_on_the_auth_settings_toggles(ctx):
    """The seeded admin logs in whether or not auth.enabled is set.

    ``bootstrap_auth`` seeds it unconditionally and ``POST /auth/login`` never
    consults auth.enabled / auth.local.enabled, so gating these steps on those
    toggles would hide a live credential. The context does not carry them at
    all — this test pins that.
    """
    state = SETUP_MACHINE.evaluate(ctx)

    assert STEP_FIRST_USER in state.steps
    assert not hasattr(ctx, "auth_enabled")


def test_the_wizard_has_no_admin_password_step(ctx):
    """Every deployment mints the admin password, so the wizard never asks for one.

    A dev deployment still on the shipped default is reported by
    ``default_credentials``, not by an outstanding step.
    """
    state = SETUP_MACHINE.evaluate(ctx)

    assert state.steps == [STEP_OIDC_PROVIDER, STEP_ORGANISATIONS, STEP_FIRST_USER]
    assert SETUP_MACHINE.status(ctx).default_credentials is True


def test_disabled_oidc_provider_does_not_satisfy_the_step(ctx):
    ctx.oidc_registry.create("entra", OIDCProvider(enabled=False, issuer="https://idp"))

    state = SETUP_MACHINE.evaluate(ctx)

    assert STEP_OIDC_PROVIDER not in state.completed_steps

    ctx.oidc_registry.update("entra", enabled=True)
    assert STEP_OIDC_PROVIDER in SETUP_MACHINE.evaluate(ctx).completed_steps


def test_the_local_admin_does_not_count_as_the_first_user(ctx):
    assert STEP_FIRST_USER not in SETUP_MACHINE.evaluate(ctx).completed_steps

    ctx.account_store.create("alice", "a-strong-user-password")
    assert STEP_FIRST_USER in SETUP_MACHINE.evaluate(ctx).completed_steps


def test_the_break_glass_account_does_not_count_as_the_first_user(ctx):
    """A gitops deployment seeds break-glass, and that must not complete the step.

    It is a shared recovery credential reconciled from the deploy repo, not an
    operator-created identity, so a deployment holding only admin and it still
    has nobody to attribute work to.
    """
    ctx.account_store.create(BREAKGLASS_USERNAME, "a-recovery-password", groups=["dfe-admins"])

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


def test_default_credentials_reads_the_config_not_the_stored_hash(ctx):
    """The admin is reconciled from config on every boot, so config is the verdict.

    A password changed only in the store is reverted at the next start, so it must
    not clear the flag.
    """
    ctx.account_store.reset_password("admin", "a-store-only-password")

    assert SETUP_MACHINE.status(ctx).default_credentials is True


def test_default_credentials_clears_once_the_password_is_minted(ctx):
    assert SETUP_MACHINE.status(ctx).default_credentials is True
    assert SETUP_MACHINE.status(_minted(ctx)).default_credentials is False


def test_context_takes_the_bootstrap_credential_from_settings(tmp_path):
    accounts = AccountStore(tmp_path / "accounts")
    accounts.create("root", "changeme", groups=["dfe-admins"])
    local = SimpleNamespace(admin_name="root", admin_password="")
    settings = SimpleNamespace(
        auth=SimpleNamespace(local=local),
        env="dev",
        gitops=SimpleNamespace(mode="team"),
        deployment=SimpleNamespace(target="docker", namespace=""),
    )
    state = SimpleNamespace(account_store=accounts, settings=settings, gitcrud=None)

    ctx = SetupContext.from_app_state(state)

    assert ctx.bootstrap_admin_name == "root"
    assert SETUP_MACHINE.status(ctx).default_credentials is True


def test_context_carries_the_deploy_kind_and_fetch_command(tmp_path):
    accounts = AccountStore(tmp_path / "accounts")
    accounts.create("admin", "changeme", groups=["dfe-admins"])
    local = SimpleNamespace(
        admin_name="",
        admin_password="",
        admin_secret_name="dfe-engine",
        admin_secret_key="admin-password",
    )
    settings = SimpleNamespace(
        auth=SimpleNamespace(local=local),
        env="dev",
        gitops=SimpleNamespace(mode="team"),
        deployment=SimpleNamespace(target="kubernetes", namespace="dfe"),
    )

    status = SETUP_MACHINE.status(
        SetupContext.from_app_state(
            SimpleNamespace(account_store=accounts, settings=settings, gitcrud=None)
        )
    )

    assert status.deploy_kind == "kubernetes"
    assert status.credential_fetch_command == (
        "kubectl -n dfe get secret dfe-engine -o jsonpath='{.data.admin-password}' | base64 -d"
    )


def test_context_falls_back_to_the_shipped_defaults_without_settings(tmp_path):
    ctx = SetupContext.from_app_state(SimpleNamespace(account_store=None))

    assert ctx.bootstrap_admin_name == "admin"
    assert ctx.bootstrap_admin_password == "changeme"
    assert ctx.default_ttl_days == 90


def test_context_reads_the_default_ttl_from_settings(tmp_path):
    settings = SimpleNamespace(
        auth=SimpleNamespace(local=SimpleNamespace(admin_name="", admin_password="")),
        env="dev",
        gitops=SimpleNamespace(mode="team"),
        deployment=SimpleNamespace(target="docker", namespace=""),
        clickhouse=SimpleNamespace(default_ttl_days=45),
    )

    ctx = SetupContext.from_app_state(SimpleNamespace(account_store=None, settings=settings))

    assert ctx.default_ttl_days == 45


def test_context_reports_the_stored_override_over_the_env_default(tmp_path):
    from dfe_engine.gitcrud import GitCrud, default_registry
    from dfe_engine.gitcrud.retention import set_stored
    from dfe_engine.gitops.repo import GitopsRepo

    crud = GitCrud(GitopsRepo(local_path=str(tmp_path / "deploy"), push=False), default_registry())
    settings = SimpleNamespace(
        auth=SimpleNamespace(local=SimpleNamespace(admin_name="", admin_password="")),
        env="dev",
        gitops=SimpleNamespace(mode="team"),
        deployment=SimpleNamespace(target="docker", namespace=""),
        clickhouse=SimpleNamespace(default_ttl_days=45),
    )
    state = SimpleNamespace(account_store=None, settings=settings, gitcrud=crud)

    assert SetupContext.from_app_state(state).default_ttl_days == 45
    set_stored(crud, 30, actor="derek")
    ctx = SetupContext.from_app_state(state)
    assert ctx.default_ttl_days == 30
    assert SETUP_MACHINE.status(ctx).default_ttl_days == 30


def test_status_carries_the_default_ttl_from_the_context(ctx):
    assert SETUP_MACHINE.status(ctx).default_ttl_days == 90
    assert SETUP_MACHINE.status(_minted(ctx, default_ttl_days=30)).default_ttl_days == 30

    # It survives completion: the console needs it long after the wizard is done.
    ctx.org_registry.create("acme")
    ctx.account_store.create("alice", "a-strong-user-password")
    done = SETUP_MACHINE.status(_minted(ctx, default_ttl_days=0))
    assert done.initial_setup.complete is True
    assert done.default_ttl_days == 0


def _with_git(ctx: SetupContext, *, merged: bool) -> SetupContext:
    return _minted(
        ctx,
        break_glass_git=AccountGitState(
            enabled=True, auto_merge=False, committed=True, merged=merged, pending=None
        ),
    )


def test_an_unmerged_deploy_repo_no_longer_blocks_setup(ctx):
    """The admin password comes from the store the deployment injects, not from git."""
    ctx.org_registry.create("acme")
    ctx.account_store.create("alice", "a-strong-user-password")

    state = SETUP_MACHINE.evaluate(_with_git(ctx, merged=False))

    assert state.complete is True
    assert state.completed_steps == [STEP_ORGANISATIONS, STEP_FIRST_USER]


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
    done = _complete(ctx)

    complete = SETUP_MACHINE.status(done)
    assert complete.initial_setup.complete is True
    assert complete.organisations == []
    assert complete.oidc_providers == []

    # ...unless the caller opts in (an authenticated admin view, say).
    unredacted = SETUP_MACHINE.status(done, redact_when_complete=False)
    assert [o.name for o in unredacted.organisations] == ["acme"]


def test_completed_setup_still_carries_the_password_hint(ctx):
    """#301: the login page needs the fetch command most once setup is long done."""
    command = (
        "kubectl -n dfe get secret dfe-engine -o jsonpath='{.data.admin-password}' | base64 -d"
    )
    ctx.org_registry.create("acme")
    ctx.account_store.create("alice", "a-strong-user-password")

    status = SETUP_MACHINE.status(
        _minted(ctx, deploy_kind="kubernetes", credential_fetch_command=command)
    )

    assert status.initial_setup.complete is True
    assert status.deploy_kind == "kubernetes"
    assert status.credential_fetch_command == command


def _complete(ctx: SetupContext) -> SetupContext:
    ctx.org_registry.create("acme")
    ctx.account_store.create("alice", "a-strong-user-password")
    return _minted(ctx)


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

    status = SETUP_MACHINE.status(_complete(ctx))

    assert status.initial_setup.complete is True
    # Name and display name, and nothing else: no issuer, no env vars, no type.
    assert [p.model_dump() for p in status.oidc_providers] == [
        {"name": "entra", "display_name": "Microsoft Entra"},
    ]
    assert "ENTRA_SECRET" not in status.model_dump_json()


def test_completed_setup_leaves_an_unset_display_name_empty(ctx):
    """No invented label — ``name`` is right there for the UI to fall back to."""
    ctx.oidc_registry.create("entra", OIDCProvider(enabled=True, issuer="https://idp"))

    assert [p.model_dump() for p in SETUP_MACHINE.status(_complete(ctx)).oidc_providers] == [
        {"name": "entra", "display_name": ""},
    ]


def test_completed_setup_drops_disabled_oidc_providers(ctx):
    """A disabled provider cannot be logged in with, so it is pure inventory."""
    ctx.oidc_registry.create("entra", OIDCProvider(enabled=True, issuer="https://idp"))
    ctx.oidc_registry.create("okta", OIDCProvider(enabled=False, issuer="https://okta"))

    assert [p.name for p in SETUP_MACHINE.status(_complete(ctx)).oidc_providers] == ["entra"]


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
