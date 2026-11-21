#  Project:      dfe-engine
#  File:         orgs/role_groups.py
#  Purpose:      The two groups each org carries, its admins and its viewers
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The two groups each org carries, its admins and its viewers, the only groups an org account may hold."""

from collections.abc import Iterable, Mapping

from dfe_engine.auth.accounts import Account
from dfe_engine.auth.groups import Group, GroupStore
from dfe_engine.auth.membership import groups_held
from dfe_engine.auth.models import ORG_ADMIN_ROLE, ORG_VIEWER_ROLE

ORG_ROLES = frozenset({ORG_ADMIN_ROLE, ORG_VIEWER_ROLE})
"""The roles an org account may hold, each only through a group tied to its own org."""


def _group_name(group: Group) -> str:
    """The name groups are sorted by."""
    return group.name


def _reaches_past(*, group: Group, org: str) -> bool:
    """Whether *group* is tied to, or names, an org other than *org*, which puts that org in its holders' org_ids."""
    tied_elsewhere = (bool(group.scope_org)) and (group.scope_org != org)
    return (tied_elsewhere) or (any(other != org for other in group.org_ids))


def _refused(*, group: Group, org: str) -> bool:
    """Whether an account of *org* may not hold *group*: one reaching past *org*, or granting any role but org roles bound where they reach only *org*."""
    if _reaches_past(group=group, org=org):
        return True
    if not (group.roles):
        return False
    # org_viewer is ``scoped``: bound at system scope it reaches only the orgs its group names, so it must name one. org_admin binds only at its org's scope.
    system_bound = not (group.scope_org)
    unbound = (system_bound) and ((not (group.org_ids)) or (ORG_ADMIN_ROLE in group.roles))
    return (unbound) or (not (set(group.roles) <= ORG_ROLES))


def org_role_breach(*, account_org: str, groups: Iterable[Group], home_org: str) -> str | None:
    """The first of *groups*, by name, an account of *account_org* may not hold, else None.

    An account of your org or of none may hold any group. An org account may hold no group reaching another org, and a group with roles only when they are all org roles reaching its own org: org_admin through a group tied to it, org_viewer also through a system group naming it alone.
    """
    if (not (account_org)) or (account_org == home_org):
        return None
    for group in sorted(groups, key=_group_name):
        if _refused(group=group, org=account_org):
            return group.name
    return None


def org_role_groups_held(
    *, account_org: str, groups: Iterable[Group], home_org: str
) -> list[Group]:
    """*groups* less every one an account of *account_org* may not hold, so its grants never reach past its org."""
    if (not (account_org)) or (account_org == home_org):
        return list(groups)
    return [group for group in groups if not (_refused(group=group, org=account_org))]


def usable_org_admins(
    *,
    accounts: Iterable[Account],
    bindings: Mapping[str, str],
    groups: list[Group],
    org: str,
) -> list[str]:
    """The accounts of *org* that can sign in holding its admins group."""
    admins = f"{org}-admins"
    return [
        account.username
        for account in accounts
        if (account.org == org)
        and (not (account.is_link_record))
        and (not (account.is_soft_deleted))
        and (account.session_denied() is None)
        and (admins in groups_held(account, groups, bindings=bindings))
    ]


def org_role_groups(*, org: str) -> dict[str, str]:
    """Each of *org*'s two groups by name, with the one role it grants."""
    return {f"{org}-admins": ORG_ADMIN_ROLE, f"{org}-viewers": ORG_VIEWER_ROLE}


def ensure_org_role_groups(*, group_store: GroupStore, org: str) -> None:
    """Create whichever of *org*'s two groups is missing, tied to *org*."""
    for name, role in org_role_groups(org=org).items():
        if group_store.get(name) is None:
            group_store.create(
                name,
                [role],
                description=f"The {role} role in org {org!r}",
                scope=f"org:{org}",
            )


def drop_org_role_groups(*, group_store: GroupStore, org: str) -> None:
    """Delete *org*'s two groups, leaving a same-named group tied to another org or none."""
    for name in org_role_groups(org=org):
        group = group_store.get(name)
        if (group is not None) and (group.scope_org == org):
            # A group with members cannot be deleted, so its members are let go first.
            if group.members:
                group_store.update(name, members=[])
            group_store.delete(name)
