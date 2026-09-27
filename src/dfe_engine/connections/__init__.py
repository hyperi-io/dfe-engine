#  Project:      dfe-engine
#  File:         connections/__init__.py
#  Purpose:      Multi-tenant ClickHouse connection management
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""ClickHouse connection resolution for DFE RBAC.

Resolves and caches a ClickHouse client per named connection
(``ConnectionRegistry``), alongside the connection config and models.
This package decides WHICH client and credentials a request uses; it
does NOT itself filter rows by org (tenant).

Org isolation is enforced by two mechanisms, neither of which lives
here:

1. Runtime view parameter -- the query executor injects the caller's
   ``org_id`` as a RESERVED bind parameter that a client cannot
   override, so a view's SQL always filters to the authenticated org
   (``dfe_engine.query.executor``, ``RESERVED_PARAMS``).
2. CH RESTRICTIVE row policies, on unless ``DFE_TENANT_ISOLATION_ENABLED``
   turns them off -- every org-pinned CH user holds ONE shared tenant role,
   and its READONLY pinned setting names the ``_org_id`` values that role's
   policies let it read (``dfe_engine.governance.ch``).

Usage::

    from dfe_engine.connections import ConnectionRegistry, ConnectionConfigLoader

    config = ConnectionConfigLoader.load_default()
    registry = ConnectionRegistry(config)
    client = registry.get_client("default")
"""

from dfe_engine.connections.config import ConnectionConfig, ConnectionConfigLoader
from dfe_engine.connections.models import ClickHouseConnection
from dfe_engine.connections.registry import ConnectionRegistry

__all__ = [
    "ClickHouseConnection",
    "ConnectionConfig",
    "ConnectionConfigLoader",
    "ConnectionRegistry",
]
