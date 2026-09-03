#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_e2e_seed.py
#  Purpose:      e2e Seed base + Account seeder (test-env gated)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

from pathlib import Path

import pytest

from dfe_engine.auth.accounts import AccountStore
from dfe_engine.auth.bootstrap import bootstrap_auth
from dfe_engine.auth.groups import GroupStore
from dfe_engine.orgs.registry import OrgRegistry


def _stores(tmp_path: Path) -> tuple[AccountStore, GroupStore, OrgRegistry]:
    """Stores as the running app hands them to the seeders.

    ``api/app.py``'s lifespan always runs ``bootstrap_auth`` before the e2e
    routes can be called, so the default groups and the break-glass admin are
    already there. Going through the real bootstrap (rather than hand-built
    empty stores) is what keeps the seeders' group names pinned to the ones
    startup actually creates -- ``_ensure_group`` raises on any drift.
    """
    account_store, group_store, *_ = bootstrap_auth(tmp_path / "config" / "auth")
    return account_store, group_store, OrgRegistry(tmp_path / "orgs")


def test_seed_refuses_when_env_is_not_test(tmp_path, monkeypatch):
    from dfe_engine.api.e2e.seed import Seed

    monkeypatch.setenv("DFE_ENV", "dev")
    accounts, groups, orgs = _stores(tmp_path)
    with pytest.raises(PermissionError, match="DFE_ENV=test"):
        Seed(account_store=accounts, group_store=groups, org_registry=orgs)


def test_seed_refuses_explicit_non_test_env_even_if_environ_is_test(tmp_path, monkeypatch):
    from dfe_engine.api.e2e.seed import Seed

    monkeypatch.setenv("DFE_ENV", "test")
    accounts, groups, orgs = _stores(tmp_path)
    with pytest.raises(PermissionError, match="DFE_ENV=test"):
        Seed(account_store=accounts, group_store=groups, org_registry=orgs, env="production")


def test_seed_allows_test_env_from_environ(tmp_path, monkeypatch):
    from dfe_engine.api.e2e.seed import Seed

    monkeypatch.setenv("DFE_ENV", "test")
    accounts, groups, orgs = _stores(tmp_path)
    Seed(account_store=accounts, group_store=groups, org_registry=orgs)


def test_seed_allows_explicit_test_env_when_environ_is_dev(tmp_path, monkeypatch):
    from dfe_engine.api.e2e.seed import Seed

    monkeypatch.setenv("DFE_ENV", "dev")
    accounts, groups, orgs = _stores(tmp_path)
    Seed(account_store=accounts, group_store=groups, org_registry=orgs, env="test")


def test_seed_dispatches_dfe_admin_user_to_accounts(tmp_path, monkeypatch):
    from dfe_engine.api.e2e.seed import Seed

    monkeypatch.setenv("DFE_ENV", "test")
    # Ambient break-glass env renames the account BOOTSTRAP seeds; it must not
    # redirect the well-known e2e account the seeder adds alongside it.
    accounts, groups, orgs = _stores(tmp_path)
    seeder = Seed(account_store=accounts, group_store=groups, org_registry=orgs)

    assert seeder.seed_static("seed_dfe_admin_user") is True
    assert seeder.seed_static("unknown") is False
    user = accounts.get("dfe_admin")
    assert user is not None
    assert "dfe-admins" in user.groups
    assert accounts.verify_password("dfe_admin", "changeme")
    assert "dfe_admin" in groups.get("dfe-admins").members
    # The bootstrap-owned break-glass account is untouched by the seed.
    assert accounts.verify_password("new-admin", "test")


def test_account_ensure_admin_joins_the_bootstrapped_group(tmp_path, monkeypatch):
    """The seeder attaches membership; bootstrap owns the group itself."""
    from dfe_engine.api.e2e.seed.accounts import Accounts

    monkeypatch.setenv("DFE_ENV", "test")
    accounts, groups, orgs = _stores(tmp_path)
    seeder = Accounts(account_store=accounts, group_store=groups, org_registry=orgs)

    assert seeder.seed_dfe_admin_user(name="playwright", password="e2e-secret") is True
    user = accounts.get("playwright")
    assert user is not None
    assert user.enabled is True
    assert "dfe-admins" in user.groups
    assert accounts.verify_password("playwright", "e2e-secret")
    admins = groups.get("dfe-admins")
    assert admins is not None
    assert admins.roles == ["admin"]
    assert "playwright" in admins.members


def test_account_ensure_admin_resets_existing(tmp_path, monkeypatch):
    from dfe_engine.api.e2e.seed.accounts import Accounts

    monkeypatch.setenv("DFE_ENV", "test")
    accounts, groups, orgs = _stores(tmp_path)
    seeder = Accounts(account_store=accounts, group_store=groups, org_registry=orgs)

    assert seeder.seed_dfe_admin_user(name="dfe_admin", password="first-pass") is True
    assert seeder.seed_dfe_admin_user(name="dfe_admin", password="second-pass") is False
    assert accounts.verify_password("dfe_admin", "second-pass")
    assert not accounts.verify_password("dfe_admin", "first-pass")


def test_account_ensure_reuses_private_upsert_for_other_groups(tmp_path, monkeypatch):
    from dfe_engine.api.e2e.seed.accounts import Accounts

    monkeypatch.setenv("DFE_ENV", "test")
    accounts, groups, orgs = _stores(tmp_path)
    seeder = Accounts(account_store=accounts, group_store=groups, org_registry=orgs)
    seeder._ensure_group("dfe-analysts")

    assert seeder._ensure("analyst", "analyst-pw", groups=["dfe-analysts"]) is True
    user = accounts.get("analyst")
    assert user is not None
    assert "dfe-analysts" in user.groups
    assert "analyst" in groups.get("dfe-analysts").members
    assert accounts.verify_password("analyst", "analyst-pw")


def test_organisation_seed_creates_default(tmp_path, monkeypatch):
    from dfe_engine.api.e2e.seed.organisations import Organisations

    monkeypatch.setenv("DFE_ENV", "test")
    accounts, groups, orgs = _stores(tmp_path)
    seeder = Organisations(account_store=accounts, group_store=groups, org_registry=orgs)

    assert seeder.seed_organisation() is True
    org = orgs.get("organisation")
    assert org is not None
    assert org.enabled is True
    assert org.display_name == "organisation"
    assert org.org_ids == ["organisation"]


def test_organisation_seed_resets_existing(tmp_path, monkeypatch):
    from dfe_engine.api.e2e.seed.organisations import Organisations

    monkeypatch.setenv("DFE_ENV", "test")
    accounts, groups, orgs = _stores(tmp_path)
    seeder = Organisations(account_store=accounts, group_store=groups, org_registry=orgs)

    orgs.create("organisation", org_ids=[], display_name="stale")
    orgs.update("organisation", enabled=False)

    assert seeder.seed_organisation(display_name="Organisation") is False
    org = orgs.get("organisation")
    assert org is not None
    assert org.enabled is True
    assert org.display_name == "Organisation"
    assert org.org_ids == ["organisation"]


def test_reset_all_recreates_break_glass_admin(tmp_path, monkeypatch):
    """reset_all must leave a loginable break-glass admin (delete_all removes every account)."""
    from dfe_engine.api.e2e.seed import Seed
    from dfe_engine.auth.bootstrap import admin_account_name, admin_account_password

    monkeypatch.setenv("DFE_ENV", "test")

    accounts, groups, orgs = _stores(tmp_path)
    seeder = Seed(account_store=accounts, group_store=groups, org_registry=orgs)

    seeder.accounts.seed_dfe_admin_user(name="extra_user")
    seeder.organisations.seed_organisation()
    assert len(accounts.list()) >= 2

    assert seeder.seed_static("reset_all") is True
    admin_name = admin_account_name()
    remaining = accounts.list()
    assert len(remaining) == 1
    assert remaining[0].username == admin_name
    assert accounts.verify_password(admin_name, admin_account_password())


def test_seed_setup_complete_seeds_organisation(tmp_path, monkeypatch):
    from dfe_engine.api.e2e.seed import Seed

    monkeypatch.setenv("DFE_ENV", "test")
    accounts, groups, orgs = _stores(tmp_path)
    seeder = Seed(account_store=accounts, group_store=groups, org_registry=orgs)

    assert seeder.seed_static("seed_setup_complete") is True
    assert accounts.get("admin") is not None
    assert accounts.verify_password("admin", "already_reset")
    assert not accounts.verify_password("admin", "changeme")
    assert accounts.get("initial_user") is not None
    assert orgs.get("organisation") is not None


def test_seed_organisation_dispatches_to_the_organisation_seeder(tmp_path, monkeypatch):
    """The script is offered in the request Literal, so it has to reach a seeder."""
    from dfe_engine.api.e2e.seed import Seed

    monkeypatch.setenv("DFE_ENV", "test")
    accounts, groups, orgs = _stores(tmp_path)
    seeder = Seed(account_store=accounts, group_store=groups, org_registry=orgs)

    assert seeder.seed_static("seed_organisation") is True
    assert orgs.get("organisation") is not None


def test_an_app_seed_without_a_deploy_repo_names_the_setting(tmp_path, monkeypatch):
    """A misconfigured e2e-server must say what is missing, not seed nothing quietly."""
    from dfe_engine.api.e2e.seed import Seed

    monkeypatch.setenv("DFE_ENV", "test")
    accounts, groups, orgs = _stores(tmp_path)
    seeder = Seed(account_store=accounts, group_store=groups, org_registry=orgs)

    with pytest.raises(RuntimeError, match="DFE_GITOPS_ENABLED"):
        seeder.seed_static("seed_app_scaling_state")


def test_reset_all_is_safe_without_a_deploy_repo(tmp_path, monkeypatch):
    """The auth-only e2e specs run on a process with gitops off; reset must not raise."""
    from dfe_engine.api.e2e.seed import Seed

    monkeypatch.setenv("DFE_ENV", "test")
    accounts, groups, orgs = _stores(tmp_path)
    seeder = Seed(account_store=accounts, group_store=groups, org_registry=orgs)

    seeder.accounts.seed_dfe_admin_user()
    assert seeder.seed_static("reset_all") is True


def test_seeders_never_create_groups_themselves(tmp_path, monkeypatch):
    """Groups are startup-bootstrap territory: a seeder refuses an unknown one.

    This is the guard that keeps `make e2e-server` honest -- the seeders may
    only join groups `dfe-engine run` already created, so an e2e process can
    never end up with a roster the product bootstrap would not produce.
    """
    from dfe_engine.api.e2e.seed.accounts import Accounts

    monkeypatch.setenv("DFE_ENV", "test")
    accounts, groups, orgs = _stores(tmp_path)
    seeder = Accounts(account_store=accounts, group_store=groups, org_registry=orgs)

    with pytest.raises(ValueError, match="dfe-nonesuch does not exist"):
        seeder._ensure_group("dfe-nonesuch")
    assert groups.get("dfe-nonesuch") is None
