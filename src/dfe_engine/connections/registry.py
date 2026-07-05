#  Project:      dfe-engine
#  File:         connections/registry.py
#  Purpose:      ConnectionRegistry — resolve and cache ClickHouse clients per role
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""ConnectionRegistry resolves ClickHouse connections for authenticated users.

Each role maps to one of the fixed CH users by privilege (the custom-settings
model): admin / infra -> the ``default`` admin connection; data_analyst -> the
read-write ``dfe_analyst``; data_analyst_ro / data_viewer / infra_ro -> the
read-only ``dfe_analyst_ro``; org_analyst -> the row-filtered ``dfe_tenant_reader``
(then wrapped in a TenantScopedClient). Privilege precedence (highest to lowest):
admin > data_analyst > data_analyst_ro > data_viewer > infra > infra_ro >
org_analyst.

Resolution is ALIAS-AWARE: every role is resolved through ``ROLE_ALIASES`` first,
so a STALE pre-rename name (e.g. ``customer_viewer``) maps to its canonical target
(``org_analyst`` -> ``tenant_reader``) instead of falling through to the admin
``default`` fallback - the phase-1 gap where a stale customer_viewer would have
been over-privileged to the admin connection.

Clients are lazily created on first use and cached for the lifetime
of the registry.
"""

from __future__ import annotations

import os
from typing import Any

from scalo.logger import logger

from dfe_engine.auth.models import AuthContext
from dfe_engine.auth.roles import ROLE_ALIASES
from dfe_engine.connections.config import ConnectionConfig
from dfe_engine.connections.models import ClickHouseConnection
from dfe_engine.connections.tenant import TenantScopedClient
from dfe_engine.governance.ch.models import ANALYST_RO_USER, TENANT_READER_USER

# Privilege ordering: highest privilege first.  When a user holds
# multiple roles, the highest-privilege connection wins.
_ROLE_PRECEDENCE: list[str] = [
    "admin",
    "data_analyst",
    "data_analyst_ro",
    "data_viewer",
    "infra",
    "infra_ro",
    "org_analyst",
]


class ConnectionRegistry:
    """Resolve and cache ClickHouse clients based on user roles.

    Args:
        config: Validated connection configuration.
        ch_host: Override every connection's host at client-creation time
            (settings-derived; see ``api.app``). The per-connection host in the
            config is a dev default - all fixed users share ONE CH cluster, so the
            host/port come from settings, only the CH USER differs by privilege.
            ``None`` keeps each connection's configured host.
        ch_port: Override every connection's port the same way.
    """

    def __init__(
        self,
        config: ConnectionConfig,
        *,
        ch_host: str | None = None,
        ch_port: int | None = None,
    ) -> None:
        self._config = config
        self._clients: dict[str, Any] = {}
        self._ch_host = ch_host
        self._ch_port = ch_port

    def get_connection_name(self, auth: AuthContext) -> str:
        """Resolve the best connection name for this user's roles.

        Alias-resolves every role (``ROLE_ALIASES``) first, then iterates roles in
        precedence order and returns the connection mapped to the highest-privilege
        role the user holds. Falls back to ``"default"`` if no role mapping matches.

        Alias-awareness is load-bearing: a stale ``customer_viewer`` resolves to
        ``org_analyst`` -> ``tenant_reader`` (row-filtered), NOT the admin
        ``default`` fallback that an unresolved unknown role lands on.

        Args:
            auth: Authenticated user context.

        Returns:
            Connection name string.
        """
        canonical = {ROLE_ALIASES.get(role, role) for role in auth.roles}
        for role in _ROLE_PRECEDENCE:
            if role in canonical and role in self._config.role_connections:
                return self._config.role_connections[role]

        # Fallback: any non-precedence role with an explicit mapping (alias-resolved
        # too), preserving the caller's role order for a deterministic pick.
        for role in auth.roles:
            resolved = ROLE_ALIASES.get(role, role)
            if resolved in self._config.role_connections:
                return self._config.role_connections[resolved]

        return "default"

    def get_client(self, connection_name: str) -> Any:
        """Get or create a clickhouse-connect client for the named connection.

        Lazily creates clients on first use.  Reads the password from
        the environment variable specified by ``password_env``.

        Args:
            connection_name: Logical connection name.

        Returns:
            A ``clickhouse_connect`` client instance.

        Raises:
            KeyError: If the connection name is not defined in config.
        """
        if connection_name in self._clients:
            return self._clients[connection_name]

        conn = self._config.connections.get(connection_name)
        if conn is None:
            raise KeyError(f"Connection '{connection_name}' not defined in config")

        password = ""
        if conn.password_env:
            password = os.environ.get(conn.password_env, "")

        import clickhouse_connect

        # Host/port come from settings (seeded at bootstrap) when provided - all
        # fixed users share ONE CH cluster; only the CH USER differs by privilege.
        host = self._ch_host or conn.host
        port = self._ch_port or conn.port

        client = clickhouse_connect.get_client(
            host=host,
            port=port,
            database=conn.database,
            username=conn.user,
            password=password,
        )

        self._clients[connection_name] = client
        logger.info(
            "Created ClickHouse client",
            connection=connection_name,
            host=host,
            port=port,
            database=conn.database,
            user=conn.user,
        )
        return client

    def get_client_for_user(self, auth: AuthContext) -> tuple[Any, list[str]]:
        """Convenience: resolve connection and return (client, org_ids).

        The caller is responsible for wrapping the client in a
        ``TenantScopedClient`` if ``org_ids`` is non-empty.

        Args:
            auth: Authenticated user context.

        Returns:
            Tuple of (clickhouse client, list of org_ids).
        """
        connection_name = self.get_connection_name(auth)
        client = self.get_client(connection_name)
        return client, auth.org_ids

    def read_client_for_user(self, auth: AuthContext) -> Any:
        """Resolve the ready-to-use direct-read client for this principal.

        The ONE acquisition point every direct-CH-read endpoint (sampler,
        discovery) shares so a read runs under the privilege-appropriate fixed CH
        user, correctly scoped. Keys the wrapping decision on the resolved CH USER
        (the row policy's actual target), NOT the connection name, so a renamed
        connection still scopes right:

          * ``dfe_tenant_reader`` (org_analyst) -> a TenantScopedClient injecting
            ``DFE_current_tenant_id`` = the caller's org_ids (empty -> '' -> zero
            rows, fail closed); readonly so the sampler's per-query settings are
            dropped.
          * ``dfe_analyst_ro`` (data_analyst_ro / data_viewer / infra_ro) -> a
            readonly TenantScopedClient with NO tenant setting (not a policy
            target); drops readonly-incompatible per-query settings.
          * every other fixed user (``dfe_admin`` / ``dfe_analyst``) -> the client
            as-is (read-write, targeted by no policy, sees all rows).

        Unlike ``get_client_for_user`` (bare client + org_ids, caller wraps), this
        returns the WRAPPED client so wrapping is decided in exactly one place.
        """
        connection_name = self.get_connection_name(auth)
        client = self.get_client(connection_name)
        conn = self._config.connections.get(connection_name)
        ch_user = conn.user if conn is not None else ""

        if ch_user == TENANT_READER_USER:
            return TenantScopedClient(client, auth.org_ids, tenant_filtered=True, readonly=True)
        if ch_user == ANALYST_RO_USER:
            return TenantScopedClient(client, [], tenant_filtered=False, readonly=True)
        return client

    def list_connections(self) -> list[ClickHouseConnection]:
        """Return all configured connection definitions.

        Returns:
            List of ClickHouseConnection models.
        """
        return list(self._config.connections.values())
