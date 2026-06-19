#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_membership.py
#  Purpose:      Tests for account/group membership sync
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

from dfe_engine.auth.accounts import AccountStore
from dfe_engine.auth.groups import GroupStore
from dfe_engine.auth.membership import sync_account_groups_for_membership_change


class TestSyncAccountGroupsForMembershipChange:
    def test_adds_group_to_account(self, tmp_path):
        accounts = AccountStore(tmp_path / "accounts")
        groups = GroupStore(tmp_path / "groups")
        groups.create("ops", roles=["viewer"])
        accounts.create("alice", "secret", groups=[])

        sync_account_groups_for_membership_change(accounts, "ops", added=["alice"])

        assert accounts.get("alice").groups == ["ops"]

    def test_removes_group_from_account(self, tmp_path):
        accounts = AccountStore(tmp_path / "accounts")
        groups = GroupStore(tmp_path / "groups")
        groups.create("ops", roles=["viewer"])
        accounts.create("alice", "secret", groups=["ops", "dev"])

        sync_account_groups_for_membership_change(accounts, "ops", removed=["alice"])

        assert accounts.get("alice").groups == ["dev"]

    def test_skips_unknown_usernames(self, tmp_path):
        accounts = AccountStore(tmp_path / "accounts")
        sync_account_groups_for_membership_change(accounts, "ops", added=["ghost"])

    def test_add_is_idempotent_for_account(self, tmp_path):
        accounts = AccountStore(tmp_path / "accounts")
        accounts.create("alice", "secret", groups=["ops"])

        sync_account_groups_for_membership_change(accounts, "ops", added=["alice"])

        assert accounts.get("alice").groups == ["ops"]
