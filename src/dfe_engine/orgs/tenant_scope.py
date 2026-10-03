#  Project:      dfe-engine
#  File:         orgs/tenant_scope.py
#  Purpose:      The tenant ids an org's rows carry, and the condition holding a read to them
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Which ``_org_id`` values an org's rows carry, and how a read is held to them.

The ClickHouse row policy pins each org's user to the org's tenant ids. A group's
``org_ids`` list org markers, each an org's name or one of its tenant ids, and
:func:`resolve_orgs` is the one rule that turns them into registered orgs: the
HyperDX connection read, the ClickHouse group bindings and every engine-side read
held to a caller's orgs (:func:`tenant_ids_for`) all go through it. The registry
refuses a write whose name or tenant ids would collide with another org's markers
(:func:`colliding_org`), so a marker only ever resolves ambiguously on data that
predates the check.
"""

from collections.abc import Iterable
from typing import Any

from dfe_engine.orgs.available_ids import ORG_ID_COLUMN


def org_tenant_ids(org: Any) -> list[str]:
    """The ``_org_id`` values an org's rows carry: its ``org_ids``, else its name.

    Args:
        org: A registered org, or anything carrying ``name`` and ``org_ids``.

    Returns:
        The org's tenant ids, in their declared order.
    """
    return list(org.org_ids) or [org.name]


def resolve_orgs(markers: Iterable[str], orgs: Iterable[Any]) -> list[Any]:
    """The registered orgs that ``markers`` name, by org name or by tenant id.

    A marker names the org of that name. Failing that, it names the one org whose
    tenant ids (:func:`org_tenant_ids`) include it. A marker no org declares, or a
    tenant id several orgs declare, names nothing, so it never widens a read.

    Args:
        markers: The org markers a caller's or a group's ``org_ids`` list.
        orgs: The org registry's entries.

    Returns:
        The orgs named, each once, in registry order; empty when no marker resolves.
    """
    registered = list(orgs)
    by_name = {org.name: org for org in registered}
    by_tenant: dict[str, list[Any]] = {}
    for org in registered:
        for tenant in org_tenant_ids(org):
            by_tenant.setdefault(tenant, []).append(org)
    named: set[str] = set()
    for marker in markers:
        org = by_name.get(marker)
        if org is None:
            declaring = by_tenant.get(marker, [])
            org = declaring[0] if len(declaring) == 1 else None
        if org is not None:
            named.add(org.name)
    return [org for org in registered if org.name in named]


def tenant_ids_for(markers: Iterable[str], orgs: Iterable[Any]) -> list[str]:
    """The tenant ids of the registered orgs that ``markers`` name (:func:`resolve_orgs`).

    Args:
        markers: The org markers a caller's groups list.
        orgs: The org registry's entries.

    Returns:
        The tenant ids, sorted and each once; empty when no marker resolves.
    """
    ids: set[str] = set()
    for org in resolve_orgs(markers, orgs):
        ids.update(org_tenant_ids(org))
    return sorted(ids)


def colliding_org(name: str, org_ids: Iterable[str], orgs: Iterable[Any]) -> Any | None:
    """The other registered org whose name or tenant ids collide with ``name``/``org_ids``.

    A write names ``name`` with declared tenant ids ``org_ids``. It collides when
    either value matches a MARKER -- the name or a tenant id -- of a different
    registered org, because each org's pinned ClickHouse user is fenced by tenant
    id, and a marker two orgs share lets both read the same rows. An org keeping
    its own name among its own tenant ids is not a collision: an org is never
    compared against itself.

    Args:
        name: The org name being created or updated.
        org_ids: The tenant ids it declares.
        orgs: The org registry's entries, including ``name``'s own current record
            when updating -- it is excluded automatically.

    Returns:
        The other org it collides with, or None.
    """
    markers = {name, *org_ids}
    for org in orgs:
        if org.name == name:
            continue
        other_markers = {org.name, *org_tenant_ids(org)}
        if markers & other_markers:
            return org
    return None


def org_condition(tenant_ids: list[str]) -> tuple[str, dict[str, Any]]:
    """The bound condition holding a read to rows whose ``_org_id`` is one of ``tenant_ids``.

    Args:
        tenant_ids: The tenant ids the read is held to.

    Returns:
        The condition, and the parameters it binds.

    Raises:
        ValueError: If ``tenant_ids`` is empty, since the read would return nothing.
    """
    if not tenant_ids:
        raise ValueError("a read held to no org would read nothing")
    return f"{ORG_ID_COLUMN} IN {{orgs:Array(String)}}", {"orgs": list(tenant_ids)}
