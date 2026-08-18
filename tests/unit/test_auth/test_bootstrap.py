#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_bootstrap.py
#  Purpose:      Tests for auth store bootstrap, including DFE_AUTH_LOCAL_ADMIN_*
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Auth store bootstrap seeds the break-glass account from DFE_AUTH_LOCAL_ADMIN_*."""

from __future__ import annotations

from pathlib import Path

from dfe_engine.auth.bootstrap import bootstrap_auth

_NAME = "DFE_AUTH_LOCAL_ADMIN_NAME"
_PASSWORD = "DFE_AUTH_LOCAL_ADMIN_PASSWORD"


def test_seed_admin_uses_dfe_auth_local_admin_name(tmp_path: Path, monkeypatch):
    monkeypatch.setenv(_NAME, "alt-admin")

    account_store, group_store, *_ = bootstrap_auth(tmp_path / "auth")

    assert account_store.get("alt-admin") is not None
    assert account_store.get("admin") is None
    group = group_store.get("dfe-admins")
    assert group is not None
    assert "alt-admin" in group.members
    assert "admin" not in group.members


def test_seed_admin_defaults_to_admin(tmp_path: Path, monkeypatch):
    monkeypatch.delenv(_NAME, raising=False)
    monkeypatch.delenv(_PASSWORD, raising=False)

    account_store, group_store, *_ = bootstrap_auth(tmp_path / "auth")

    assert account_store.get("admin") is not None
    group = group_store.get("dfe-admins")
    assert group is not None
    assert "admin" in group.members
    assert account_store.verify_password("admin", "changeme")


def test_seed_admin_uses_dfe_auth_local_admin_password(tmp_path: Path, monkeypatch):
    monkeypatch.delenv(_NAME, raising=False)
    monkeypatch.setenv(_PASSWORD, "custom-admin-pw-that-is-plenty-long")

    account_store, *_ = bootstrap_auth(tmp_path / "auth")

    assert account_store.verify_password("admin", "custom-admin-pw-that-is-plenty-long")
    assert not account_store.verify_password("admin", "changeme")


def test_dfe_auth_local_admin_password_beats_changeme_override(tmp_path: Path, monkeypatch):
    monkeypatch.delenv(_NAME, raising=False)
    monkeypatch.setenv(_PASSWORD, "custom-admin-pw-that-is-plenty-long")

    account_store, *_ = bootstrap_auth(tmp_path / "auth", default_admin_password="changeme")

    assert account_store.verify_password("admin", "custom-admin-pw-that-is-plenty-long")


def test_explicit_password_beats_dfe_auth_local_admin_password(tmp_path: Path, monkeypatch):
    monkeypatch.delenv(_NAME, raising=False)
    monkeypatch.setenv(_PASSWORD, "from-env")

    account_store, *_ = bootstrap_auth(tmp_path / "auth", default_admin_password="from-arg")

    assert account_store.verify_password("admin", "from-arg")
