#  Project:      dfe-engine
#  File:         governance/ch/bootstrap.py
#  Purpose:      Wire settings + the RBAC stores into a CH-RBAC reconcile
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The shared assembly between settings/stores and ``reconcile_ch_rbac``.

Both callers - app startup and the governance endpoint - need the same admin
client, secrets store, orgs and bindings, and previously built all four inline;
the copies drifted, which is how ``bindings`` came to be passed by neither.

Building the client stays a separate call so each caller keeps its own failure
policy: startup logs and continues, the endpoint returns 503.

Imports are function-local because the rest of the ``governance.ch`` package is
pure rendering with no cluster or settings dependency.
"""

from __future__ import annotations

from typing import Any

from .bindings import derive_group_bindings
from .reconciler import ReconcileResult, reconcile_ch_rbac


def ch_admin_client(settings: Any) -> Any:
    """An admin ClickHouse client built from settings. Raises if unreachable."""
    from dfe_engine.clickhouse.clickhouse_manager import ClickHouseManager

    ch_cfg = {
        "ch_host": settings.clickhouse.host,
        "ch_port": settings.clickhouse.port,
        "ch_username": settings.clickhouse.username,
        "ch_password": settings.clickhouse.password,
        "ch_secure": settings.clickhouse.secure,
        "ch_verify": settings.clickhouse.verify,
        "ch_ca_cert": settings.clickhouse.ca_cert,
    }
    return ClickHouseManager.get_instance(ch_cfg).get_clickhouse_client()._client


def reconcile_from_stores(
    admin_client: Any,
    *,
    settings: Any,
    org_registry: Any = None,
    group_store: Any = None,
) -> ReconcileResult:
    """Reconcile CH RBAC from the org registry and the RBAC group store.

    A missing store contributes nothing rather than failing: an org registry with
    no groups still reconciles tiers, roles and row policies.
    """
    from dfe_engine.secrets import build_secrets

    orgs = org_registry.list() if org_registry is not None else []
    groups = group_store.list() if group_store is not None else []
    return reconcile_ch_rbac(
        admin_client,
        secrets_store=build_secrets(settings.secrets),
        orgs=orgs,
        bindings=derive_group_bindings(groups, orgs),
    )
