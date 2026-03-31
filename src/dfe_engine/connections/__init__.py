#  Project:      dfe-engine
#  File:         connections/__init__.py
#  Purpose:      Multi-tenant ClickHouse connection management
#  Language:     Python
#
#  License:      FSL-1.1-ALv2
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Multi-tenant ClickHouse connection management.

Provides connection resolution, tenant-scoped query execution, and
ClickHouse user/policy reconciliation for DFE RBAC.

The custom settings pattern uses a small number of static CH users
(3-5 by privilege level) with row policies based on
``getSetting('current_tenant_id')``.  dfe-engine injects the tenant
ID per query based on the authenticated user's org_ids.

Usage::

    from dfe_engine.connections import ConnectionRegistry, ConnectionConfigLoader

    config = ConnectionConfigLoader.load_default()
    registry = ConnectionRegistry(config)
    client, org_ids = registry.get_client_for_user(auth_context)
"""

from dfe_engine.connections.config import ConnectionConfig, ConnectionConfigLoader
from dfe_engine.connections.models import ClickHouseConnection
from dfe_engine.connections.reconciler import Reconciler, ReconcileResult
from dfe_engine.connections.registry import ConnectionRegistry
from dfe_engine.connections.tenant import TenantScopedClient

__all__ = [
    "ClickHouseConnection",
    "ConnectionConfig",
    "ConnectionConfigLoader",
    "ConnectionRegistry",
    "Reconciler",
    "ReconcileResult",
    "TenantScopedClient",
]
