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
from dfe_engine.auth.groups import GroupStore
from dfe_engine.orgs.registry import OrgRegistry


def _stores(tmp_path: Path) -> tuple[AccountStore, GroupStore, OrgRegistry]:
    return (
        AccountStore(tmp_path / "accounts"),
        GroupStore(tmp_path / "groups"),
        OrgRegistry(tmp_path / "orgs"),
    )


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


def test_seed_dispatches_seed_admin_to_accounts(tmp_path, monkeypatch):
    from dfe_engine.api.e2e.seed import Seed

    monkeypatch.setenv("DFE_ENV", "test")
    # Ambient break-glass env must not redirect the well-known e2e admin.
    monkeypatch.setenv("DFE_AUTH_LOCAL_ADMIN_NAME", "new-admin")
    monkeypatch.setenv("DFE_AUTH_LOCAL_ADMIN_PASSWORD", "test")
    accounts, groups, orgs = _stores(tmp_path)
    seeder = Seed(account_store=accounts, group_store=groups, org_registry=orgs)

    assert seeder.seed_static("seed_admin") is True
    assert seeder.seed_static("unknown") is False
    user = accounts.get("admin")
    assert user is not None
    assert accounts.get("new-admin") is None
    assert "dfe-admins" in user.groups
    assert accounts.verify_password("admin", "changeme")
    assert "admin" in groups.get("dfe-admins").members


def test_account_ensure_admin_creates_group_and_membership(tmp_path, monkeypatch):
    from dfe_engine.api.e2e.seed.accounts import Accounts

    monkeypatch.setenv("DFE_ENV", "test")
    accounts, groups, orgs = _stores(tmp_path)
    seeder = Accounts(account_store=accounts, group_store=groups, org_registry=orgs)

    assert seeder.seed_admin(name="playwright", password="e2e-secret") is True
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

    assert seeder.seed_admin(name="admin", password="first-pass") is True
    assert seeder.seed_admin(name="admin", password="second-pass") is False
    assert accounts.verify_password("admin", "second-pass")
    assert not accounts.verify_password("admin", "first-pass")


def test_account_ensure_reuses_private_upsert_for_other_groups(tmp_path, monkeypatch):
    from dfe_engine.api.e2e.seed.accounts import Accounts

    monkeypatch.setenv("DFE_ENV", "test")
    accounts, groups, orgs = _stores(tmp_path)
    seeder = Accounts(account_store=accounts, group_store=groups, org_registry=orgs)
    seeder._ensure_group(
        "dfe-analysts", roles=["data_analyst"], description="Hunt, query, source CRUD"
    )

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
