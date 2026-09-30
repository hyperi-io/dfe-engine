#  Project:      dfe-engine
#  File:         auth/membership.py
#  Purpose:      Keep Account.groups in sync with Group.members
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Which groups an account holds, and keeping ``Account.groups`` and ``Group.members`` in step."""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping

from dfe_engine.auth.accounts import Account, AccountStore
from dfe_engine.auth.groups import Group, GroupStore


def linked_providers(names: Iterable[str], bindings: Mapping[str, str]) -> frozenset[str]:
    """The provider names *names* answer for: each one, and each that a binding joins to it.

    ``auth.source_provider_bindings`` declares a SCIM stamp and an OIDC provider one
    identity source, so a group either of them linked is linked for both.

    Args:
        names: Provider names or source stamps, empties ignored.
        bindings: ``auth.source_provider_bindings``, stamp to OIDC provider name.

    Returns:
        The names, plus every stamp bound to one of them and every provider one is bound to.
    """
    own = {name for name in names if name}
    related = {target for stamp, target in bindings.items() if stamp in own}
    related |= {stamp for stamp, target in bindings.items() if target in own}
    return frozenset(name for name in own | related if name)


def linked_groups(
    identifiers: Iterable[str], groups: Iterable[Group], providers: Collection[str]
) -> list[Group]:
    """The stored groups an identity provider's asserted *identifiers* are linked to.

    A group is linked when its ``source_id`` is the identifier and it names no provider
    or one of *providers*. Its name never links: a directory user can pick a group's
    display name, but not the id the directory assigns it. The OIDC group sync links
    by the same rule.

    Args:
        identifiers: What the IdP asserted: group ids, or names where it sends names.
        groups: Every stored group, listed once by the caller.
        providers: The providers the assertion answers for (:func:`linked_providers`).

    Returns:
        The linked groups, in stored order.
    """
    wanted = set(identifiers)
    return [
        group
        for group in groups
        if group.source_id
        and group.source_id in wanted
        and (not group.source_provider or group.source_provider in providers)
    ]


def groups_held(
    account: Account, groups: Iterable[Group], *, bindings: Mapping[str, str]
) -> list[str]:
    """The names of the groups *account* holds, which its roles resolve from (never a token).

    The group files are the authority, so a member removed there loses the group even
    while the account's own list still names it. An IdP-owned account's own list is
    what its IdP asserts, and holds only the groups those identifiers are linked to
    (:func:`linked_groups`), beside any an operator added it to by hand.

    Args:
        account: The account.
        groups: Every stored group, listed once by the caller.
        bindings: ``auth.source_provider_bindings``.

    Returns:
        Sorted group names.
    """
    listed = list(groups)
    held = {group.name for group in listed if account.username in group.members}
    if account.source_provider:
        providers = linked_providers([account.source_provider], bindings)
        held.update(group.name for group in linked_groups(account.groups, listed, providers))
    return sorted(held)


def groups_named(identifiers: Iterable[str], groups: Iterable[Group]) -> list[Group]:
    """The stored groups *identifiers* name, each by group name else by provider source_id.

    The lookup role resolution makes, so these are the groups whose roles those
    identifiers carry. An identifier that names no group is left out.

    Args:
        identifiers: Group names or IdP-asserted provider ids.
        groups: Every stored group, listed once by the caller.

    Returns:
        The named groups, each once, in the order first named.
    """
    listed = list(groups)
    by_name = {group.name: group for group in listed}
    by_source_id = {group.source_id: group for group in listed if group.source_id}
    named: dict[str, Group] = {}
    for identifier in identifiers:
        group = by_name.get(identifier) or by_source_id.get(identifier)
        if group is not None:
            named.setdefault(group.name, group)
    return list(named.values())


def forget_member(group_store: GroupStore, username: str) -> None:
    """Remove *username* from every group that lists it.

    Membership is keyed by name, so without this an account recreated under the name
    would inherit every group the deleted one held.
    """
    for group in group_store.list():
        if username in group.members:
            group_store.remove_member(group.name, username)


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
