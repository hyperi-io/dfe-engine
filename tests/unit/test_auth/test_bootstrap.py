#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_bootstrap.py
#  Purpose:      Tests for auth store bootstrap (break-glass admin + named seeds)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Auth store bootstrap seeds the break-glass admin and named seed accounts."""

from __future__ import annotations

from pathlib import Path

from dfe_engine.auth.bootstrap import bootstrap_auth, seeded_account_email
from dfe_engine.settings import SeedAccount


def test_seed_admin_defaults_to_admin(tmp_path: Path, monkeypatch):
    account_store, group_store, *_ = bootstrap_auth(tmp_path / "auth")

    assert account_store.get("admin") is not None
    group = group_store.get("dfe-admins")
    assert group is not None
    assert "admin" in group.members
    assert account_store.verify_password("admin", "changeme")
    assert account_store.get("admin").email == "admin@dfe.local"


def test_bootstrap_backfills_admin_email_when_missing(tmp_path: Path):
    auth_dir = tmp_path / "auth"
    account_store, *_ = bootstrap_auth(auth_dir)
    account_store.update("admin", email="")

    account_store, *_ = bootstrap_auth(auth_dir)
    assert account_store.get("admin").email == "admin@dfe.local"


def test_seeded_account_email_prefers_recovery_email():
    assert seeded_account_email("admin") == "admin@dfe.local"
    assert seeded_account_email("admin", "ops@example.com") == "ops@example.com"
    assert seeded_account_email("admin", "  ops@example.com  ") == "ops@example.com"


def test_recovery_email_seeds_admin(tmp_path: Path):
    account_store, *_ = bootstrap_auth(tmp_path / "auth", recovery_email="ops@example.com")

    assert account_store.get("admin").email == "ops@example.com"


def test_recovery_email_reconciles_admin_over_fallback(tmp_path: Path):
    auth_dir = tmp_path / "auth"
    account_store, *_ = bootstrap_auth(auth_dir)
    assert account_store.get("admin").email == "admin@dfe.local"

    account_store, *_ = bootstrap_auth(auth_dir, recovery_email="ops@example.com")
    assert account_store.get("admin").email == "ops@example.com"


def test_explicit_password_beats_the_default(tmp_path: Path, monkeypatch):
    account_store, *_ = bootstrap_auth(tmp_path / "auth", default_admin_password="from-arg")

    assert account_store.verify_password("admin", "from-arg")


# ── Named seed accounts (dfe-infra #106) ─────────────────────────


def test_seed_account_created_with_password_and_groups(tmp_path: Path, monkeypatch):
    seeds = [SeedAccount(username="kay", password="kay-password-long", groups=["dfe-analysts"])]

    account_store, group_store, *_ = bootstrap_auth(tmp_path / "auth", seed_accounts=seeds)

    kay = account_store.get("kay")
    assert kay is not None
    assert kay.groups == ["dfe-analysts"]
    assert account_store.verify_password("kay", "kay-password-long")
    assert "kay" in group_store.get("dfe-analysts").members


def test_seed_account_password_reconciles_on_rebuild(tmp_path: Path, monkeypatch):
    auth_dir = tmp_path / "auth"

    bootstrap_auth(
        auth_dir,
        seed_accounts=[
            SeedAccount(username="kaz", password="old-password-long", groups=["dfe-analysts"])
        ],
    )
    # A rebuild against the SAME store with a changed config password: config wins.
    account_store, *_ = bootstrap_auth(
        auth_dir,
        seed_accounts=[
            SeedAccount(username="kaz", password="new-password-long", groups=["dfe-analysts"])
        ],
    )

    assert account_store.verify_password("kaz", "new-password-long")
    assert not account_store.verify_password("kaz", "old-password-long")


def test_seed_account_groups_reconcile_on_rebuild(tmp_path: Path, monkeypatch):
    auth_dir = tmp_path / "auth"

    bootstrap_auth(
        auth_dir,
        seed_accounts=[
            SeedAccount(username="kay", password="kay-password-long", groups=["dfe-analysts"])
        ],
    )
    account_store, group_store, *_ = bootstrap_auth(
        auth_dir,
        seed_accounts=[
            SeedAccount(username="kay", password="kay-password-long", groups=["dfe-viewers"])
        ],
    )

    assert set(account_store.get("kay").groups) == {"dfe-viewers"}
    assert "kay" in group_store.get("dfe-viewers").members
    assert "kay" not in group_store.get("dfe-analysts").members


def test_seed_account_skips_break_glass_admin_name(tmp_path: Path, monkeypatch):
    # A seed spec colliding with the break-glass admin must not clobber its password.
    seeds = [SeedAccount(username="admin", password="hijack-attempt-long", groups=["dfe-viewers"])]

    account_store, *_ = bootstrap_auth(tmp_path / "auth", seed_accounts=seeds)

    assert account_store.verify_password("admin", "changeme")
    assert not account_store.verify_password("admin", "hijack-attempt-long")


def test_seed_account_skips_the_configured_break_glass_name(tmp_path: Path, monkeypatch):
    # DFE_AUTH_LOCAL_ADMIN_NAME renames the break-glass account, and the reconcile
    # has to skip THAT name -- skipping "admin" would leave the real one clobbered.
    seeds = [SeedAccount(username="root", password="hijack-attempt-long", groups=["dfe-viewers"])]

    account_store, *_ = bootstrap_auth(
        tmp_path / "auth", default_admin_name="root", seed_accounts=seeds
    )

    assert account_store.verify_password("root", "changeme")
    assert not account_store.verify_password("root", "hijack-attempt-long")


def test_a_seed_named_admin_is_created_when_the_break_glass_is_renamed(tmp_path: Path, monkeypatch):
    # The mirror image: with the break-glass renamed, "admin" is an ordinary name.
    seeds = [SeedAccount(username="admin", password="admin-seed-password", groups=["dfe-viewers"])]

    account_store, *_ = bootstrap_auth(
        tmp_path / "auth", default_admin_name="root", seed_accounts=seeds
    )

    assert account_store.verify_password("admin", "admin-seed-password")


def test_seed_account_unknown_group_is_skipped_not_fatal(tmp_path: Path, monkeypatch):
    seeds = [SeedAccount(username="kay", password="kay-password-long", groups=["no-such-group"])]

    account_store, *_ = bootstrap_auth(tmp_path / "auth", seed_accounts=seeds)

    kay = account_store.get("kay")
    assert kay is not None
    assert kay.groups == []
