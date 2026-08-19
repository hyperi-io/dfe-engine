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


def _stores(tmp_path: Path) -> tuple[AccountStore, GroupStore]:
    return AccountStore(tmp_path / "accounts"), GroupStore(tmp_path / "groups")


def test_seed_refuses_when_env_is_not_test(tmp_path, monkeypatch):
    from dfe_engine.api.e2e.seed import Seed

    monkeypatch.setenv("DFE_ENV", "dev")
    accounts, groups = _stores(tmp_path)
    with pytest.raises(PermissionError, match="DFE_ENV=test"):
        Seed(account_store=accounts, group_store=groups)


def test_seed_refuses_explicit_non_test_env_even_if_environ_is_test(tmp_path, monkeypatch):
    from dfe_engine.api.e2e.seed import Seed

    monkeypatch.setenv("DFE_ENV", "test")
    accounts, groups = _stores(tmp_path)
    with pytest.raises(PermissionError, match="DFE_ENV=test"):
        Seed(account_store=accounts, group_store=groups, env="production")


def test_seed_allows_test_env_from_environ(tmp_path, monkeypatch):
    from dfe_engine.api.e2e.seed import Seed

    monkeypatch.setenv("DFE_ENV", "test")
    accounts, groups = _stores(tmp_path)
    Seed(account_store=accounts, group_store=groups)


def test_seed_allows_explicit_test_env_when_environ_is_dev(tmp_path, monkeypatch):
    from dfe_engine.api.e2e.seed import Seed

    monkeypatch.setenv("DFE_ENV", "dev")
    accounts, groups = _stores(tmp_path)
    Seed(account_store=accounts, group_store=groups, env="test")


def test_seed_dispatches_seed_admin_to_accounts(tmp_path, monkeypatch):
    from dfe_engine.api.e2e.seed import Seed

    monkeypatch.setenv("DFE_ENV", "test")
    # Ambient break-glass env must not redirect the well-known e2e admin.
    monkeypatch.setenv("DFE_AUTH_LOCAL_ADMIN_NAME", "new-admin")
    monkeypatch.setenv("DFE_AUTH_LOCAL_ADMIN_PASSWORD", "test")
    accounts, groups = _stores(tmp_path)
    seeder = Seed(account_store=accounts, group_store=groups)

    assert seeder.seed("seed_admin") is True
    assert seeder.seed("unknown") is False
    user = accounts.get("admin")
    assert user is not None
    assert accounts.get("new-admin") is None
    assert "dfe-admins" in user.groups
    assert accounts.verify_password("admin", "changeme")
    assert "admin" in groups.get("dfe-admins").members


def test_account_ensure_admin_creates_group_and_membership(tmp_path, monkeypatch):
    from dfe_engine.api.e2e.seed.accounts import Accounts

    monkeypatch.setenv("DFE_ENV", "test")
    accounts, groups = _stores(tmp_path)
    seeder = Accounts(account_store=accounts, group_store=groups)

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
    accounts, groups = _stores(tmp_path)
    seeder = Accounts(account_store=accounts, group_store=groups)

    assert seeder.seed_admin(name="admin", password="first-pass") is True
    assert seeder.seed_admin(name="admin", password="second-pass") is False
    assert accounts.verify_password("admin", "second-pass")
    assert not accounts.verify_password("admin", "first-pass")


def test_account_ensure_reuses_private_upsert_for_other_groups(tmp_path, monkeypatch):
    from dfe_engine.api.e2e.seed.accounts import Accounts

    monkeypatch.setenv("DFE_ENV", "test")
    accounts, groups = _stores(tmp_path)
    seeder = Accounts(account_store=accounts, group_store=groups)
    seeder._ensure_group(
        "dfe-analysts", roles=["data_analyst"], description="Hunt, query, source CRUD"
    )

    assert seeder._ensure("analyst", "analyst-pw", groups=["dfe-analysts"]) is True
    user = accounts.get("analyst")
    assert user is not None
    assert "dfe-analysts" in user.groups
    assert "analyst" in groups.get("dfe-analysts").members
    assert accounts.verify_password("analyst", "analyst-pw")
