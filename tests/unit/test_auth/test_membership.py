#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_membership.py
#  Purpose:      Tests for account/group membership sync
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Membership sync, and the groups an account holds.

An IdP assertion takes a stored group only through the id the group is linked to.
A directory user can name a group ``dfe-admins``, so a name links nothing. A group
is linked when its ``source_id`` is the asserted identifier and it names no provider
or one the assertion answers for.
"""

import pytest

from dfe_engine.auth.accounts import Account, AccountStore
from dfe_engine.auth.groups import Group, GroupStore
from dfe_engine.auth.membership import (
    groups_held,
    groups_held_after,
    groups_named,
    linked_groups,
    linked_providers,
    sync_account_groups_for_membership_change,
    sync_group_members_for_account_groups_change,
)


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


class TestSyncGroupMembersForAccountGroupsChange:
    def test_adds_member_to_group(self, tmp_path):
        accounts = AccountStore(tmp_path / "accounts")
        groups = GroupStore(tmp_path / "groups")
        groups.create("ops", roles=["viewer"])
        accounts.create("alice", "secret", groups=["ops"])

        sync_group_members_for_account_groups_change(groups, "alice", added=["ops"])

        assert "alice" in groups.get("ops").members

    def test_removes_member_from_group(self, tmp_path):
        groups = GroupStore(tmp_path / "groups")
        groups.create("ops", roles=["viewer"])
        groups.create("dev", roles=["viewer"])
        groups.add_member("ops", "alice")

        sync_group_members_for_account_groups_change(groups, "alice", removed=["ops"])

        assert "alice" not in groups.get("ops").members

    def test_skips_unknown_groups(self, tmp_path):
        groups = GroupStore(tmp_path / "groups")
        sync_group_members_for_account_groups_change(groups, "alice", added=["missing"])


def _group(name: str, *, source_id: str = "", source_provider: str = "", members=()) -> Group:
    return Group(
        name=name,
        roles=["admin"],
        members=list(members),
        source_id=source_id,
        source_provider=source_provider,
    )


def _idp_account(groups: list[str], provider: str = "entra") -> Account:
    return Account(
        username="jane-corp-com",
        password_hash="!",
        groups=groups,
        external=True,
        source_provider=provider,
    )


class TestLinkedProviders:
    def test_a_name_answers_for_itself(self):
        assert linked_providers(["entra"], {}) == {"entra"}

    def test_a_binding_joins_a_stamp_and_its_provider_both_ways(self):
        bindings = {"scim": "entra"}
        assert linked_providers(["scim"], bindings) == {"scim", "entra"}
        assert linked_providers(["entra"], bindings) == {"scim", "entra"}

    def test_an_unrelated_binding_adds_nothing(self):
        assert linked_providers(["okta"], {"scim": "entra"}) == {"okta"}

    def test_empty_names_are_ignored(self):
        assert linked_providers(["", "entra"], {"": "entra"}) == {"entra"}


class TestLinkedGroups:
    def test_a_group_of_the_asserted_name_is_not_linked(self):
        groups = [_group("dfe-admins")]
        assert linked_groups(["dfe-admins"], groups, {"entra"}) == []

    def test_the_group_whose_source_id_is_asserted_is_linked(self):
        linked = _group("platform-admins", source_id="0295f72c")
        assert linked_groups(["0295f72c"], [_group("dfe-admins"), linked], {"entra"}) == [linked]

    def test_a_group_naming_no_provider_links_for_any(self):
        linked = _group("dfe-admins", source_id="dfe-admins")
        assert linked_groups(["dfe-admins"], [linked], {"okta"}) == [linked]

    @pytest.mark.parametrize(("provider", "expected"), [("entra", 1), ("okta", 0)])
    def test_a_group_naming_a_provider_links_for_that_one_only(self, provider, expected):
        linked = _group("dfe-admins", source_id="g-1", source_provider="entra")
        assert len(linked_groups(["g-1"], [linked], {provider})) == expected

    def test_an_empty_identifier_links_nothing(self):
        assert linked_groups([""], [_group("unlinked")], {"entra"}) == []


class TestGroupsHeld:
    def test_membership_holds_by_name(self):
        account = Account(username="bob", password_hash="!", groups=[])
        groups = [_group("dfe-analysts", members=["bob"])]
        assert groups_held(account, groups, bindings={}) == ["dfe-analysts"]

    def test_a_local_accounts_own_list_holds_nothing(self):
        account = Account(username="ops", password_hash="!", groups=["dfe-admins"])
        groups = [_group("dfe-admins", source_id="dfe-admins")]
        assert groups_held(account, groups, bindings={}) == []

    def test_an_idp_account_holds_the_groups_its_ids_are_linked_to(self):
        account = _idp_account(["0295f72c", "dfe-admins"])
        groups = [_group("dfe-admins"), _group("platform-admins", source_id="0295f72c")]
        assert groups_held(account, groups, bindings={}) == ["platform-admins"]

    def test_a_bound_stamp_takes_the_providers_groups(self):
        account = _idp_account(["g-1"], provider="scim")
        groups = [_group("entra-admins", source_id="g-1", source_provider="entra")]
        assert groups_held(account, groups, bindings={}) == []
        assert groups_held(account, groups, bindings={"scim": "entra"}) == ["entra-admins"]

    def test_membership_and_linked_groups_are_both_held(self):
        account = _idp_account(["g-1"])
        groups = [
            _group("by-hand", members=["jane-corp-com"]),
            _group("by-link", source_id="g-1"),
        ]
        assert groups_held(account, groups, bindings={}) == ["by-hand", "by-link"]


class TestGroupsHeldAfter:
    def test_an_idp_accounts_new_list_is_its_assertion_and_its_membership(self):
        account = _idp_account(["DFE-Admins"])
        groups = [_group("DFE-Admins"), _group("dfe-admins", source_id="DFE-Admins")]

        held = groups_held_after(
            account=account, added=["DFE-Admins"], bindings={}, groups=groups, removed=[]
        )

        assert held == ["DFE-Admins", "dfe-admins"]

    def test_a_removed_membership_is_no_longer_held(self):
        account = Account(groups=[], password_hash="!", username="bob")
        groups = [_group("dfe-analysts", members=["bob", "carol"])]

        held = groups_held_after(
            account=account, added=[], bindings={}, groups=groups, removed=["dfe-analysts"]
        )

        assert (held, groups[0].members) == ([], ["bob", "carol"])


class TestGroupsNamed:
    def test_a_provider_id_names_no_group(self):
        groups = [_group("dfe-admins", source_id="DFE-Admins"), _group("dfe-viewers")]

        named = groups_named(groups=groups, identifiers=["DFE-Admins", "dfe-viewers"])

        assert [group.name for group in named] == ["dfe-viewers"]
