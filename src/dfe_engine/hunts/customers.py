#  Project:      dfe-engine
#  File:         hunts/customers.py
#  Purpose:      The orgs a hunt reads, by name, and the tenant ids each resolved to
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A hunt's ``customers`` are org names, and its ``customer_tenant_ids`` what each resolved to.

The engine resolves each name through the org registry when it writes the hunt
(:func:`~dfe_engine.orgs.tenant_scope.tenant_ids_by_org`) and stores the result
beside the names, because the hunt runner reads the deploy repo and the registry
is not in it. Every org create, update and delete stores that org's tenant ids
afresh in each hunt naming it (:func:`with_org_tenant_ids`), and a deleted org's
entry goes. The runner refuses every fire of a hunt holding a customer with no
stored tenant ids, so its watermark waits for the hunt to be edited.
"""

from typing import Any

CUSTOMER_TENANT_IDS = "customer_tenant_ids"
"""The hunt YAML key holding each customer's tenant ids, written by the engine."""


def hunt_customers(definition: dict[str, Any]) -> list[str]:
    """The org names a hunt reads: its ``customers``, each once.

    Args:
        definition: The parsed hunt YAML.

    Returns:
        The non-blank customer strings, stripped, in their given order. Empty when
        the hunt names none, or ``customers`` is not a list.
    """
    customers = definition.get("customers")
    if not isinstance(customers, list):
        return []
    named = [c.strip() for c in customers if isinstance(c, str) and c.strip()]
    return list(dict.fromkeys(named))


def hunt_tenant_ids(definition: dict[str, Any]) -> tuple[list[str], list[str]]:
    """The ``_org_id`` values a hunt reads, and the customers with none stored.

    Args:
        definition: The parsed hunt YAML.

    Returns:
        The tenant ids of every resolved customer, sorted and each once, and the
        customers whose ``customer_tenant_ids`` entry is missing, empty or not a
        list of strings, in their given order.
    """
    stored = definition.get(CUSTOMER_TENANT_IDS)
    resolved = stored if isinstance(stored, dict) else {}
    tenant_ids: set[str] = set()
    unresolved: list[str] = []
    for customer in hunt_customers(definition):
        entry = resolved.get(customer)
        ids = (
            [i.strip() for i in entry if isinstance(i, str) and i.strip()]
            if isinstance(entry, list)
            else []
        )
        if ids:
            tenant_ids.update(ids)
        else:
            unresolved.append(customer)
    return sorted(tenant_ids), unresolved


def with_org_tenant_ids(
    definition: dict[str, Any], org: str, tenant_ids: list[str] | None
) -> dict[str, Any] | None:
    """The hunt with ``org``'s stored tenant ids set to ``tenant_ids``, or None if unchanged.

    Only ``org``'s entry moves, so a hunt naming several orgs keeps the others as
    stored. A hunt that does not name ``org`` is unchanged.

    Args:
        definition: The parsed hunt YAML.
        org: The org that was created, updated or deleted.
        tenant_ids: The org's tenant ids now
            (:func:`~dfe_engine.orgs.tenant_scope.org_tenant_ids`), or None for a
            deleted org, whose entry goes so the hunt fails as ``unknown_customers``
            rather than read the rows the org left behind.

    Returns:
        The hunt to store, or None when it does not name ``org`` or already holds
        exactly this.
    """
    if org not in hunt_customers(definition):
        return None
    stored = definition.get(CUSTOMER_TENANT_IDS)
    resolved = dict(stored) if isinstance(stored, dict) else {}
    if tenant_ids is None:
        if org not in resolved:
            return None
        del resolved[org]
    elif resolved.get(org) == tenant_ids:
        return None
    else:
        resolved[org] = list(tenant_ids)
    return {**definition, CUSTOMER_TENANT_IDS: resolved}
