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
from typing import TYPE_CHECKING, Any

from scalo.logger import logger

from dfe_engine.clickhouse.tls import resolve_clickhouse_tls
from dfe_engine.connections.config import ConnectionConfig

if TYPE_CHECKING:
    from dfe_engine.settings import ClickHouseSettings


class ConnectionRegistry:
    """Resolve and cache ClickHouse clients by connection name.

    Every named connection is a different CH user on the SAME cluster the
    engine's own ``ClickHouseSettings`` describes (RBAC scoping, not a
    different server), so they all take that cluster's TLS posture.

    Args:
        config: Validated connection configuration.
        clickhouse: The engine's ClickHouse settings (secure/verify/ca_cert).
            Read lazily from ``get_settings()`` when omitted.
    """

    def __init__(
        self,
        config: ConnectionConfig,
        clickhouse: ClickHouseSettings | None = None,
    ) -> None:
        self._config = config
        self._clickhouse = clickhouse
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

        ch = self._clickhouse
        if ch is None:
            from dfe_engine.settings import get_settings

            ch = get_settings().clickhouse
        tls = resolve_clickhouse_tls(secure=ch.secure, verify=ch.verify, ca_cert=ch.ca_cert)

        import clickhouse_connect

        client = clickhouse_connect.get_client(
            host=conn.host,
            port=conn.port,
            database=conn.database,
            username=conn.user,
            password=password,
            **tls.connect_kwargs(),
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
