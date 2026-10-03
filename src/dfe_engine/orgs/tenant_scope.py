#  Project:      dfe-engine
#  File:         orgs/tenant_scope.py
#  Purpose:      The tenant ids an org's rows carry, and the condition holding a read to them
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Which ``_org_id`` values an org's rows carry, and how a read is held to them.

The ClickHouse row policy pins each org's user to the org's tenant ids, and an
engine-side read that holds a caller to its orgs binds the same ids, resolved by
:func:`tenant_ids_for` from the org names the caller's groups list.
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


def tenant_ids_for(org_names: Iterable[str], orgs: Iterable[Any]) -> list[str]:
    """The tenant ids of the registered orgs named in ``org_names``.

    A name that is not a registered org resolves to nothing, as the ClickHouse
    group bindings skip it, so a held read never binds a value no org declares.

    Args:
        org_names: The org names a caller's groups list.
        orgs: The org registry's entries.

    Returns:
        The tenant ids, sorted and each once; empty when no name resolves.
    """
    by_name = {org.name: org for org in orgs}
    ids: set[str] = set()
    for name in org_names:
        org = by_name.get(name)
        if org is not None:
            ids.update(org_tenant_ids(org))
    return sorted(ids)


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
