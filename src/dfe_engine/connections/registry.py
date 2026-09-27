#  Project:      dfe-engine
#  File:         connections/registry.py
#  Purpose:      ConnectionRegistry -- resolve and cache ClickHouse clients per role
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""ConnectionRegistry resolves ClickHouse clients by connection name.

Clients are lazily created on first use and cached for the lifetime
of the registry.
"""

import os
from typing import Any

from scalo.logger import logger

from dfe_engine.connections.config import ConnectionConfig
from dfe_engine.connections.models import ClickHouseConnection


class ConnectionRegistry:
    """Resolve and cache ClickHouse clients by connection name.

    Args:
        config: Validated connection configuration.
    """

    def __init__(self, config: ConnectionConfig) -> None:
        self._config = config
        self._clients: dict[str, Any] = {}

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

    def list_connections(self) -> list[ClickHouseConnection]:
        """Return all configured connection definitions.

        Returns:
            List of ClickHouseConnection models.
        """
        return list(self._config.connections.values())
