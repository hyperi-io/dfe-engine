#  Project:      dfe-engine
#  File:         auth/membership.py
#  Purpose:      Keep Account.groups in sync with Group.members
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Synchronise ``Account.groups`` and ``Group.members``."""

from __future__ import annotations

from collections.abc import Iterable

from dfe_engine.auth.accounts import AccountStore
from dfe_engine.auth.groups import GroupStore


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
