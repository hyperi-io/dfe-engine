#  Project:      dfe-engine
#  File:         connections/registry.py
#  Purpose:      ConnectionRegistry — resolve and cache ClickHouse clients per role
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""ConnectionRegistry resolves ClickHouse connections for authenticated users.

Connection resolution uses the role_connections mapping to find the
best connection for a given user's roles.  Privilege precedence
(highest to lowest): admin > data_analyst > data_analyst_viewer >
data_viewer > infra_admin > infra_viewer > customer_viewer.

Clients are lazily created on first use and cached for the lifetime
of the registry.
"""

from __future__ import annotations

import os
from typing import Any

from scalo.logger import logger

from dfe_engine.auth.models import AuthContext
from dfe_engine.connections.config import ConnectionConfig
from dfe_engine.connections.models import ClickHouseConnection

# Privilege ordering: highest privilege first.  When a user holds
# multiple roles, the highest-privilege connection wins.
_ROLE_PRECEDENCE: list[str] = [
    "admin",
    "data_analyst",
    "data_analyst_viewer",
    "data_viewer",
    "infra_admin",
    "infra_viewer",
    "customer_viewer",
]


class ConnectionRegistry:
    """Resolve and cache ClickHouse clients based on user roles.

    Args:
        config: Validated connection configuration.
    """

    def __init__(self, config: ConnectionConfig) -> None:
        self._config = config
        self._clients: dict[str, Any] = {}

    def get_connection_name(self, auth: AuthContext) -> str:
        """Resolve the best connection name for this user's roles.

        Iterates roles in precedence order and returns the connection
        mapped to the highest-privilege role the user holds.  Falls
        back to ``"default"`` if no role mapping matches.

        Args:
            auth: Authenticated user context.

        Returns:
            Connection name string.
        """
        for role in _ROLE_PRECEDENCE:
            if role in auth.roles and role in self._config.role_connections:
                return self._config.role_connections[role]

        # Fallback: check any non-precedence roles the user may have
        for role in auth.roles:
            if role in self._config.role_connections:
                return self._config.role_connections[role]

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

        client = clickhouse_connect.get_client(
            host=conn.host,
            port=conn.port,
            database=conn.database,
            username=conn.user,
            password=password,
        )

        self._clients[connection_name] = client
        logger.info(
            "Created ClickHouse client",
            connection=connection_name,
            host=conn.host,
            port=conn.port,
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

    def list_connections(self) -> list[ClickHouseConnection]:
        """Return all configured connection definitions.

        Returns:
            List of ClickHouseConnection models.
        """
        return list(self._config.connections.values())
