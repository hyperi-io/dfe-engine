#  Project:      dfe-engine
#  File:         auth/membership.py
#  Purpose:      Keep Account.groups in sync with Group.members
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Synchronise account group lists when group membership changes."""

from __future__ import annotations

from collections.abc import Iterable

from dfe_engine.auth.accounts import AccountStore


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
