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

Platform roles win at system scope: a SYSTEM group holding a declared, unscoped
role other than the org viewer role reads UNRESTRICTED, even when a domain rule or
``org_ids`` tie it to an org. The org filter exists to fence tenants in, not to
fence the platform's own analysts out. A role marked ``scoped`` is a tenant role
wherever it is bound, and an org-scoped group's roles bind at that org's scope
only, so either gets its org's pinned user.

A group's org markers resolve to registered orgs by name or by tenant id, as the
HyperDX connection read and the sampler resolve them
(:func:`~dfe_engine.orgs.tenant_scope.resolve_orgs`). A group whose markers name
no registered org fails closed - it is skipped and gets no ClickHouse user at all,
because the alternative is an unrestricted user, and an unrestricted user reads
EVERY org's rows.
"""

from typing import Any

from scalo.logger import logger

from dfe_engine.auth.models import Scope, ScopedGrant, platform_grants
from dfe_engine.auth.roles import RoleConfig
from dfe_engine.orgs.tenant_scope import resolve_orgs

from .models import GroupChBinding

# Engine roles whose groups also read the otel database (every org's platform
# telemetry), held at system scope; analysts and org-scoped groups never do.
OTEL_READER_ROLES = {"admin", "infra_admin"}


def derive_group_bindings(
    groups: list[Any], orgs: list[Any], *, role_config: RoleConfig | None = None
) -> list[GroupChBinding]:
    """Map RBAC groups onto CH bindings, dropping any that cannot be expressed.

    ``groups`` are ``auth.groups.Group`` records and ``orgs`` the org registry's
    entries. A group's claimed orgs are its owning org (for ``org:``-scoped
    groups) plus its ``org_ids``, matching how ``api.deps`` resolves the same
    membership for an auth context. ``role_config`` is the role definitions
    ``authorize()`` reads, so a ``scoped`` role fences here as it does in the
    API; None reads the shipped ones.

    The tier is left empty so the reconciler resolves it to the default analyst
    tier - the least-privilege end of the tier list.
    """
    bindings: list[GroupChBinding] = []

    for group in groups:
        scoped = {group.scope_org} if group.scope_org else set()
        claimed = scoped | set(group.org_ids)

        roles = set(getattr(group, "roles", []) or [])
        # Bound where api.deps binds them: the owning org's scope, else system-wide.
        grant_scope = Scope(type="org", id=group.scope_org) if group.scope_org else Scope()
        grants = [ScopedGrant(role=role, scope=grant_scope) for role in sorted(roles)]
        platform_roles = {grant.role for grant in platform_grants(grants, role_config=role_config)}
        if claimed and platform_roles:
            logger.info(
                "group holds system-scope platform roles; its CH user is unrestricted "
                "despite org markers",
                group=group.name,
                roles=sorted(roles),
            )
            claimed = set()

        resolved = {org.name for org in resolve_orgs(claimed, orgs)}
        if claimed and not resolved:
            logger.error(
                "group's org markers name no registered org; skipping its CH user",
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
