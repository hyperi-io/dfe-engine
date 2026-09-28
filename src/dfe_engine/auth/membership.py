#  Project:      dfe-engine
#  File:         auth/membership.py
#  Purpose:      Keep Account.groups in sync with Group.members
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Which groups an account holds, and keeping ``Account.groups`` and ``Group.members`` in step."""

from __future__ import annotations

from collections.abc import Iterable

from dfe_engine.auth.accounts import Account, AccountStore
from dfe_engine.auth.groups import Group, GroupStore


def groups_held(account: Account, groups: Iterable[Group]) -> list[str]:
    """The groups *account* holds, by the identifier its roles resolve from (never a token).

    The group files are the authority, so a member removed there loses the group even
    while the account's own list still names it. An IdP-owned account also holds its
    own list: that is what its IdP asserts, as names or as provider ids, so a group an
    operator adds it to by hand is held beside those, not in place of them.

    Args:
        account: The account.
        groups: Every stored group, listed once by the caller.

    Returns:
        Sorted group identifiers, each a group name or an IdP-asserted provider id.
    """
    held = {group.name for group in groups if account.username in group.members}
    if account.source_provider:
        held.update(account.groups)
    return sorted(held)


def sync_account_groups_for_membership_change(
    account_store: AccountStore,
    group_name: str,
    *,
    added: Iterable[str] = (),
    removed: Iterable[str] = (),
) -> None:
    """Update ``Account.groups`` after ``Group.members`` changes.

    Only local accounts that exist are updated. Unknown usernames are skipped.
    """
    for username in added:
        account = account_store.get(username)
        if account is None or group_name in account.groups:
            continue
        account_store.update(username, groups=[*account.groups, group_name])

    for username in removed:
        account = account_store.get(username)
        if account is None or group_name not in account.groups:
            continue
        account_store.update(
            username,
            groups=[g for g in account.groups if g != group_name],
        )


def sync_group_members_for_account_groups_change(
    group_store: GroupStore,
    username: str,
    *,
    added: Iterable[str] = (),
    removed: Iterable[str] = (),
) -> None:
    """Update ``Group.members`` after ``Account.groups`` changes.

    Unknown groups are skipped (no error).
    """
    for group_name in added:
        try:
            group_store.add_member(group_name, username)
        except KeyError:
            continue

    for group_name in removed:
        try:
            group_store.remove_member(group_name, username)
        except KeyError:
            continue
