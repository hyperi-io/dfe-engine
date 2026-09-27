#  Project:      dfe-engine
#  File:         governance/ch/bindings.py
#  Purpose:      Derive group -> (tier, org) CH bindings from the RBAC group store
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Derive the CH bindings the reconciler renders group users from.

Groups already carry the org axis (``scope: org:<name>`` plus ``org_ids``), so
bindings are DERIVED rather than configured separately - one source of truth, and
an org-scoped group cannot drift from its ClickHouse user.

Platform roles win at system scope: a SYSTEM group holding any role other than the
org viewer role reads UNRESTRICTED, even when a domain rule or ``org_ids`` tie it to
an org. The org filter exists to fence tenants in, not to fence the platform's own
analysts out. An org-scoped group's roles bind at that org's scope only, so it
always gets its org's pinned user, whatever roles it holds.

A group claiming an org that is not registered fails closed - it is skipped and
gets no ClickHouse user at all, because the alternative is an unrestricted user,
and an unrestricted user reads EVERY org's rows.
"""

from typing import Any

from scalo.logger import logger

from dfe_engine.auth.models import Scope, ScopedGrant, platform_grants

from .models import GroupChBinding

# Engine roles whose groups also read the otel database (every org's platform
# telemetry), held at system scope; analysts and org-scoped groups never do.
OTEL_READER_ROLES = {"admin", "infra_admin"}


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

        roles = set(getattr(group, "roles", []) or [])
        # Bound where api.deps binds them: the owning org's scope, else system-wide.
        grant_scope = Scope(type="org", id=group.scope_org) if group.scope_org else Scope()
        grants = [ScopedGrant(role=role, scope=grant_scope) for role in sorted(roles)]
        platform_roles = {grant.role for grant in platform_grants(grants)}
        if claimed and platform_roles:
            logger.info(
                "group holds system-scope platform roles; its CH user is unrestricted "
                "despite org markers",
                group=group.name,
                roles=sorted(roles),
            )
            claimed = set()

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
                "group resolves to several orgs; skipping its CH user - "
                "model the span as one org with many org_ids",
                group=group.name,
                orgs=sorted(resolved),
            )
            continue

        bindings.append(
            GroupChBinding(
                group=group.name,
                org=next(iter(resolved), ""),
                ch_roles=["otel_reader"] if platform_roles & OTEL_READER_ROLES else [],
            )
        )

    return bindings
