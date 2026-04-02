#  Project:      dfe-engine
#  File:         hyperdx/client.py
#  Purpose:      HyperDX internal API client for team and connection management
#  Language:     Python
#
#  License:      FSL-1.1-ALv2
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""HyperDX internal API client.

Manages HyperDX teams and ClickHouse connections via the HyperDX
internal API.  All operations are **non-fatal**: if HyperDX is
unreachable, failures are logged as warnings and the caller proceeds
normally.

Usage::

    from dfe_engine.hyperdx.client import HyperDXClient

    client = HyperDXClient(base_url="http://hyperdx:8080", api_key="secret")
    team_id = await client.create_team("customer-acme")
    if team_id:
        await client.create_connection(team_id, "default", ...)
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from hyperi_pylib.logger import logger

from dfe_engine.connections.config import ConnectionConfig
from dfe_engine.orgs.models import Org


@dataclass
class SyncResult:
    """Result of a full HyperDX reconciliation pass.

    Attributes:
        teams_created: Names of teams that were created.
        teams_failed: Names of teams that failed to create.
        connections_created: Number of connections created.
        connections_failed: Number of connections that failed.
    """

    teams_created: list[str] = field(default_factory=list)
    teams_failed: list[str] = field(default_factory=list)
    connections_created: int = 0
    connections_failed: int = 0


class HyperDXClient:
    """Manage HyperDX teams and connections via internal API.

    All operations are non-fatal: if HyperDX is unreachable, log a
    warning and return gracefully.  Callers should not depend on success.
    """

    def __init__(self, base_url: str, api_key: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._connected = True  # Optimistic; set False on first failure

    async def create_team(self, name: str) -> str | None:
        """Create a HyperDX team.

        Args:
            name: Team name (e.g. "customer-acme").

        Returns:
            Team ID string on success, None on failure.
        """
        if not self._connected:
            logger.warning("HyperDX unreachable, skipping create_team", team=name)
            return None

        try:
            from hyperi_pylib.http import AsyncHttpClient

            async with AsyncHttpClient(base_url=self._base_url) as client:
                response = await client.post(
                    "/api/v1/teams",
                    json={"name": name},
                    headers=self._headers(),
                )
                response.raise_for_status()
                data = response.json()
                team_id = data.get("id", "")
                logger.info("HyperDX team created", team=name, team_id=team_id)
                return team_id
        except Exception as exc:
            self._connected = False
            logger.warning(
                "HyperDX create_team failed (non-fatal)",
                team=name,
                error=str(exc),
            )
            return None

    async def create_connection(
        self,
        team_id: str,
        name: str,
        host: str,
        port: int,
        database: str,
        user: str,
        password: str,
    ) -> str | None:
        """Create a ClickHouse connection on a HyperDX team.

        Args:
            team_id: HyperDX team ID.
            name: Connection name.
            host: ClickHouse hostname.
            port: ClickHouse HTTP port.
            database: ClickHouse database.
            user: ClickHouse username.
            password: ClickHouse password.

        Returns:
            Connection ID string on success, None on failure.
        """
        if not self._connected:
            logger.warning(
                "HyperDX unreachable, skipping create_connection",
                team_id=team_id,
                connection=name,
            )
            return None

        try:
            from hyperi_pylib.http import AsyncHttpClient

            async with AsyncHttpClient(base_url=self._base_url) as client:
                response = await client.post(
                    f"/api/v1/teams/{team_id}/connections",
                    json={
                        "name": name,
                        "host": host,
                        "port": port,
                        "database": database,
                        "user": user,
                        "password": password,
                    },
                    headers=self._headers(),
                )
                response.raise_for_status()
                data = response.json()
                conn_id = data.get("id", "")
                logger.info(
                    "HyperDX connection created",
                    team_id=team_id,
                    connection=name,
                    conn_id=conn_id,
                )
                return conn_id
        except Exception as exc:
            self._connected = False
            logger.warning(
                "HyperDX create_connection failed (non-fatal)",
                team_id=team_id,
                connection=name,
                error=str(exc),
            )
            return None

    async def delete_connection(self, team_id: str, conn_id: str) -> bool:
        """Delete a ClickHouse connection from a HyperDX team.

        Args:
            team_id: HyperDX team ID.
            conn_id: Connection ID to delete.

        Returns:
            True on success, False on failure.
        """
        if not self._connected:
            logger.warning(
                "HyperDX unreachable, skipping delete_connection",
                team_id=team_id,
                conn_id=conn_id,
            )
            return False

        try:
            from hyperi_pylib.http import AsyncHttpClient

            async with AsyncHttpClient(base_url=self._base_url) as client:
                response = await client.delete(
                    f"/api/v1/teams/{team_id}/connections/{conn_id}",
                    headers=self._headers(),
                )
                response.raise_for_status()
                logger.info(
                    "HyperDX connection deleted",
                    team_id=team_id,
                    conn_id=conn_id,
                )
                return True
        except Exception as exc:
            self._connected = False
            logger.warning(
                "HyperDX delete_connection failed (non-fatal)",
                team_id=team_id,
                conn_id=conn_id,
                error=str(exc),
            )
            return False

    async def delete_team(self, team_id: str) -> bool:
        """Delete a HyperDX team.

        Args:
            team_id: HyperDX team ID to delete.

        Returns:
            True on success, False on failure.
        """
        if not self._connected:
            logger.warning(
                "HyperDX unreachable, skipping delete_team",
                team_id=team_id,
            )
            return False

        try:
            from hyperi_pylib.http import AsyncHttpClient

            async with AsyncHttpClient(base_url=self._base_url) as client:
                response = await client.delete(
                    f"/api/v1/teams/{team_id}",
                    headers=self._headers(),
                )
                response.raise_for_status()
                logger.info("HyperDX team deleted", team_id=team_id)
                return True
        except Exception as exc:
            self._connected = False
            logger.warning(
                "HyperDX delete_team failed (non-fatal)",
                team_id=team_id,
                error=str(exc),
            )
            return False

    async def update_connection(
        self,
        team_id: str,
        connection_id: str,
        **kwargs: object,
    ) -> bool:
        """Update a ClickHouse connection on a HyperDX team.

        Args:
            team_id: HyperDX team ID.
            connection_id: Connection ID to update.
            **kwargs: Connection fields to update (e.g. host, port, password).

        Returns:
            True on success, False on failure.
        """
        if not self._connected:
            logger.warning(
                "HyperDX unreachable, skipping update_connection",
                team_id=team_id,
                connection_id=connection_id,
            )
            return False

        try:
            from hyperi_pylib.http import AsyncHttpClient

            async with AsyncHttpClient(base_url=self._base_url) as client:
                response = await client.put(
                    f"/api/v1/teams/{team_id}/connections/{connection_id}",
                    json=kwargs,
                    headers=self._headers(),
                )
                response.raise_for_status()
                logger.info(
                    "HyperDX connection updated",
                    team_id=team_id,
                    connection_id=connection_id,
                )
                return True
        except Exception as exc:
            self._connected = False
            logger.warning(
                "HyperDX update_connection failed (non-fatal)",
                team_id=team_id,
                connection_id=connection_id,
                error=str(exc),
            )
            return False

    async def sync_connections(
        self,
        orgs: list[Org],
        conn_config: ConnectionConfig,
    ) -> SyncResult:
        """Full reconciliation: ensure HyperDX teams match the org registry.

        Creates a team for each enabled org (prefixed with ``customer-``)
        and ensures it has the tenant_reader connection from conn_config.

        Args:
            orgs: List of orgs from OrgRegistry.
            conn_config: Connection configuration with connection definitions.

        Returns:
            SyncResult summarising what happened.
        """
        result = SyncResult()

        tenant_conn = conn_config.connections.get("tenant_reader")
        if tenant_conn is None:
            logger.warning("No 'tenant_reader' connection in config, skipping sync")
            return result

        for org in orgs:
            if not org.enabled:
                continue

            team_name = f"customer-{org.name}"
            team_id = await self.create_team(team_name)
            if team_id is None:
                result.teams_failed.append(team_name)
                continue
            result.teams_created.append(team_name)

            password = os.environ.get(tenant_conn.password_env, "")
            conn_id = await self.create_connection(
                team_id=team_id,
                name=tenant_conn.name,
                host=tenant_conn.host,
                port=tenant_conn.port,
                database=tenant_conn.database,
                user=tenant_conn.user,
                password=password,
            )
            if conn_id is None:
                result.connections_failed += 1
            else:
                result.connections_created += 1

        return result

    def generate_default_connections_json(
        self,
        conn_config: ConnectionConfig,
    ) -> str:
        """Generate DEFAULT_CONNECTIONS env var JSON for HyperDX Helm chart.

        Produces a JSON array of connection objects suitable for the
        HyperDX ``DEFAULT_CONNECTIONS`` environment variable.

        Args:
            conn_config: Connection configuration.

        Returns:
            JSON string of connection definitions.
        """
        connections = []
        for name, conn in conn_config.connections.items():
            password = os.environ.get(conn.password_env, "")
            connections.append(
                {
                    "name": name,
                    "host": conn.host,
                    "port": conn.port,
                    "database": conn.database,
                    "user": conn.user,
                    "password": password,
                }
            )
        return json.dumps(connections)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        """Return authorization headers for HyperDX API calls."""
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
