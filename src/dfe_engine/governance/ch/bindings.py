#  Project:      dfe-engine
#  File:         governance/ch/bindings.py
#  Purpose:      Derive group -> (tier, org) CH bindings from the RBAC group store
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Derive the CH bindings the reconciler grants org roles from.

A binding is what turns an RBAC group into a ClickHouse identity: without one the
reconciler creates org roles and row policies that nothing ever holds, so every
account keeps seeing every org's rows. Groups already carry the org axis
(``scope: org:<name>`` plus ``org_ids``), so bindings are DERIVED rather than
configured separately - one source of truth, and an org-scoped group cannot drift
from its ClickHouse user.

An org's visibility is one RESTRICTIVE row policy per table, and ClickHouse ANDs
the restrictive policies that apply to a user. Two org roles therefore yield
``_org_id = 'a' AND _org_id = 'b'`` and the user sees NOTHING, so a group may
resolve to at most one org. Access spanning several tenant ids is modelled as an
ORG carrying several ``org_ids``, which renders a single ``IN`` predicate.

Both unresolvable cases fail closed - the group is skipped and gets no ClickHouse
user at all, because the alternative is a user with no org role, and a user
holding no org role is targeted by no policy and sees EVERY org's rows.
"""

from __future__ import annotations

from typing import Any

from scalo.logger import logger

from .models import GroupChBinding


def derive_group_bindings(groups: list[Any], orgs: list[Any]) -> list[GroupChBinding]:
    """Map RBAC groups onto CH bindings, dropping any that cannot be expressed.

    ``groups`` are ``auth.groups.Group`` records and ``orgs`` the org registry's
    entries. A group's claimed orgs are its owning org (for ``org:``-scoped
    groups) plus its ``org_ids``, matching how ``api.deps`` resolves the same
    membership for an auth context.

    The tier is left empty so the reconciler resolves it to the default analyst
    tier - the least-privilege end of the tier list.
    """
    org_names = {o.name for o in orgs}
    bindings: list[GroupChBinding] = []

    for group in groups:
        scoped = {group.scope_org} if group.scope_org else set()
        claimed = scoped | set(group.org_ids)
        resolved = claimed & org_names

        if claimed and not resolved:
            logger.error(
                "group claims orgs that are not registered; skipping its CH user",
                group=group.name,
                claimed=sorted(claimed),
            )
            continue

        if len(resolved) > 1:
            logger.error(
                "group resolves to several orgs, which ClickHouse cannot express; "
                "skipping its CH user - model the span as one org with many org_ids",
                group=group.name,
                orgs=sorted(resolved),
            )
            continue

        bindings.append(GroupChBinding(group=group.name, org=next(iter(resolved), "")))

    return bindings
