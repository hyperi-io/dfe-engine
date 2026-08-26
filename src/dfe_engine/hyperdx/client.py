#  Project:      dfe-engine
#  File:         hyperdx/client.py
#  Purpose:      HyperDX control-API client shaped to the dfe-hyperdx fork's surface
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""HyperDX control-API client, shaped to the dfe-hyperdx fork's REAL surface.

The fork's control API is session-scoped: every call acts on the team of the
authenticated principal. The engine authenticates as its machine identity
(``svc:dfe-engine``), which the fork maps to the deployment's default team and
JIT-creates on first contact. There are NO ``/api/v1/teams`` routes and no team
create/delete endpoints; the reachable surface is::

    GET  /team                    the caller's team (JIT-creates the default team)
    POST /team/invitation         invite an email to the caller's team
    GET/POST /connections         ClickHouse connections on the caller's team
    PUT/DELETE /connections/:id
    GET/POST /sources             telemetry sources on the caller's team
    PUT/DELETE /sources/:id

All operations are **non-fatal**: if HyperDX is unreachable, failures are
logged as warnings and the caller proceeds normally.

Usage::

    from dfe_engine.hyperdx.client import HyperDXClient

    client = HyperDXClient(
        base_url="http://hyperdx:8080",
        token_provider=machine_token_source.token,
    )
    team = await client.get_team()
    conn_id = await client.ensure_connection(
        name="tenant_reader",
        host="clickhouse",
        port=8123,
        username="tenant_reader",
        password="secret",
    )
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from scalo.logger import logger

from dfe_engine.connections.config import ConnectionConfig

# This is a non-fatal side channel called inside interactive API requests
# (org create), so an unreachable fork must cost seconds, not the
# AsyncHttpClient default of 30s x 3 retries.
_TIMEOUT_SECONDS = 5.0
_RETRIES = 1
# After a failure the breaker re-probes once this window elapses, so a fork
# that boots after the engine recovers without an engine restart.
_RETRY_AFTER_SECONDS = 60.0


def _connection_host_url(host: str, port: int | None) -> str:
    """Collapse engine host+port into the fork's single URL ``host`` field."""
    if "://" in host:
        return host
    if port:
        return f"http://{host}:{port}"
    return f"http://{host}"


@dataclass
class SyncResult:
    """Result of a HyperDX reconciliation pass.

    Attributes:
        teams_created: Names of teams confirmed present (JIT-created or existing).
        teams_failed: Team lookups that failed.
        connections_created: Number of connections confirmed or created.
        connections_failed: Number of connections that failed.
    """

    teams_created: list[str] = field(default_factory=list)
    teams_failed: list[str] = field(default_factory=list)
    connections_created: int = 0
    connections_failed: int = 0


class HyperDXClient:
    """Manage the fork's session-scoped team, connections and sources.

    All operations are non-fatal: if HyperDX is unreachable, log a
    warning and return gracefully.  Callers should not depend on success.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        token_provider: Callable[[], str] | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        # Engine-minted machine JWT supplier; wins over any static api_key.
        self._token_provider = token_provider
        self._connected = True  # Optimistic; set False on first failure
        # Consulted only while disconnected; inf means "never re-probe", so a
        # manually latched breaker stays latched while a real failure sets a
        # finite deadline.
        self._retry_at = float("inf")

    # ------------------------------------------------------------------
    # Team (the fork exposes ONE team per authenticated principal)
    # ------------------------------------------------------------------

    async def get_team(self) -> dict[str, Any] | None:
        """Fetch the caller's team; the fork JIT-creates the default team here.

        Returns:
            Team dict (``_id``, ``name``, ``apiKey``, ...) or None on failure.
        """
        data = await self._request("get", "/team", op="get_team")
        return data if isinstance(data, dict) else None

    async def get_team_api_key(self) -> str | None:
        """Return the caller's team API key, or None on failure."""
        team = await self.get_team()
        if not team:
            return None
        return team.get("apiKey") or None

    async def invite_member(self, email: str) -> bool:
        """Invite a user to the caller's team via ``POST /team/invitation``.

        Args:
            email: Email address to invite.

        Returns:
            True on success, False on failure (non-fatal).
        """
        data = await self._request(
            "post",
            "/team/invitation",
            json_body={"email": email},
            op="invite_member",
            email=email,
        )
        if data is None:
            return False
        logger.info("HyperDX member invited", email=email)
        return True

    # ------------------------------------------------------------------
    # Connections (fork ConnectionSchema: name, host URL, username, password)
    # ------------------------------------------------------------------

    async def list_connections(self) -> list[dict[str, Any]] | None:
        """List the caller's team connections, or None on failure."""
        data = await self._request("get", "/connections", op="list_connections")
        return data if isinstance(data, list) else None

    async def create_connection(
        self,
        *,
        name: str,
        host: str,
        username: str,
        password: str = "",
        port: int | None = None,
    ) -> str | None:
        """Create a ClickHouse connection on the caller's team.

        Args:
            name: Connection name.
            host: ClickHouse host (bare hostname or full URL).
            username: ClickHouse username (the fork field is ``username``, not ``user``).
            password: ClickHouse password.
            port: ClickHouse HTTP port, folded into the URL when ``host`` is bare.

        Returns:
            Connection ID string on success, None on failure.
        """
        body = {
            "name": name,
            "host": _connection_host_url(host, port),
            "username": username,
            "password": password,
        }
        data = await self._request(
            "post",
            "/connections",
            json_body=body,
            op="create_connection",
            connection=name,
        )
        if not isinstance(data, dict):
            return None
        conn_id = str(data.get("id", ""))
        if conn_id:
            logger.info("HyperDX connection created", connection=name, conn_id=conn_id)
        return conn_id or None

    async def ensure_connection(
        self,
        *,
        name: str,
        host: str,
        username: str,
        password: str = "",
        port: int | None = None,
    ) -> str | None:
        """Create the named connection unless it already exists (fork does not dedupe).

        Returns:
            Existing or new connection ID, None on failure.
        """
        existing = await self.list_connections()
        if existing:
            for conn in existing:
                if conn.get("name") == name:
                    return str(conn.get("id") or conn.get("_id") or "") or None
        return await self.create_connection(
            name=name, host=host, username=username, password=password, port=port
        )

    async def update_connection(self, connection_id: str, connection: dict[str, Any]) -> bool:
        """Update a connection; the fork validates the FULL schema including ``id``.

        Args:
            connection_id: Connection ID to update.
            connection: Full connection body (``id`` is injected from the argument).

        Returns:
            True on success, False on failure.
        """
        body = {**connection, "id": connection_id}
        data = await self._request(
            "put",
            f"/connections/{connection_id}",
            json_body=body,
            op="update_connection",
            connection_id=connection_id,
        )
        return data is not None

    async def delete_connection(self, connection_id: str) -> bool:
        """Delete a connection from the caller's team.

        Returns:
            True on success, False on failure.
        """
        data = await self._request(
            "delete",
            f"/connections/{connection_id}",
            op="delete_connection",
            connection_id=connection_id,
        )
        return data is not None

    # ------------------------------------------------------------------
    # Sources (fork SourceSchema: kind-discriminated union, passed through)
    # ------------------------------------------------------------------

    async def list_sources(self) -> list[dict[str, Any]] | None:
        """List the caller's team sources, or None on failure."""
        data = await self._request("get", "/sources", op="list_sources")
        return data if isinstance(data, list) else None

    async def create_source(self, source: dict[str, Any]) -> dict[str, Any] | None:
        """Create a source on the caller's team.

        Args:
            source: Fork ``SourceSchemaNoId`` body (``name``, ``kind``,
                ``connection``, ``from``, ``timestampValueExpression``, ...).

        Returns:
            The created source document, or None on failure.
        """
        data = await self._request(
            "post",
            "/sources",
            json_body=source,
            op="create_source",
            source=str(source.get("name", "")),
        )
        return data if isinstance(data, dict) else None

    async def update_source(self, source_id: str, source: dict[str, Any]) -> bool:
        """Update a source; the fork validates the FULL schema including ``id``.

        Returns:
            True on success, False on failure.
        """
        body = {**source, "id": source_id}
        data = await self._request(
            "put",
            f"/sources/{source_id}",
            json_body=body,
            op="update_source",
            source_id=source_id,
        )
        return data is not None

    async def delete_source(self, source_id: str) -> bool:
        """Delete a source from the caller's team.

        Returns:
            True on success, False on failure.
        """
        data = await self._request(
            "delete",
            f"/sources/{source_id}",
            op="delete_source",
            source_id=source_id,
        )
        return data is not None

    # ------------------------------------------------------------------
    # Saved searches
    # ------------------------------------------------------------------

    async def saved_search_sql(self, saved_search_id: str) -> dict[str, Any] | None:
        """Render a saved search to the ClickHouse SELECT that view actually runs.

        The fork owns the rendering: the chart-config renderer and the column
        metadata it resolves against are both TypeScript, and a second renderer
        here would be a second definition of what a view means.

        Returns:
            ``{"rawSql", "sql", "savedSearchName", "source"}``, or None on failure.
        """
        data = await self._request(
            "get",
            f"/dfe/saved-search/{saved_search_id}/sql",
            op="saved_search_sql",
            saved_search_id=saved_search_id,
        )
        return data if isinstance(data, dict) else None

    # ------------------------------------------------------------------
    # Reconciliation + chart seeding
    # ------------------------------------------------------------------

    async def sync_connections(self, conn_config: ConnectionConfig) -> SyncResult:
        """Reconcile the default team: present and holding ``tenant_reader``.

        The fork exposes no multi-team management, so reconciliation is:
        confirm the caller's (default) team, then ensure the ``tenant_reader``
        connection exists on it.

        Args:
            conn_config: Connection configuration with connection definitions.

        Returns:
            SyncResult summarising what happened.
        """
        result = SyncResult()

        tenant_conn = conn_config.connections.get("tenant_reader")
        if tenant_conn is None:
            logger.warning("No 'tenant_reader' connection in config, skipping sync")
            return result

        team = await self.get_team()
        if not team:
            result.teams_failed.append("default")
            return result
        result.teams_created.append(str(team.get("name", "default")))

        conn_id = await self.ensure_connection(
            name=tenant_conn.name,
            host=tenant_conn.host,
            port=tenant_conn.port,
            username=tenant_conn.user,
            password=os.environ.get(tenant_conn.password_env, ""),
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
        """Generate DEFAULT_CONNECTIONS env JSON in the fork's Connection shape.

        The fork's ``setupDefaults`` spreads each entry into its Connection
        model, which knows ``username`` and a single URL ``host`` - emitting
        ``user`` or separate port/database fields silently drops the credential
        and falls back to the ClickHouse default user (dfe-engine#145).

        Args:
            conn_config: Connection configuration.

        Returns:
            JSON string of connection definitions.
        """
        connections = []
        for name, conn in conn_config.connections.items():
            connections.append(
                {
                    "name": name,
                    "host": _connection_host_url(conn.host, conn.port),
                    "username": conn.user,
                    "password": os.environ.get(conn.password_env, ""),
                }
            )
        return json.dumps(connections)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        op: str,
        **log_fields: object,
    ) -> Any | None:
        """One non-fatal HTTP round trip; None (and ``_connected=False``) on failure."""
        if not self._connected:
            if time.monotonic() < self._retry_at:
                logger.warning("HyperDX unreachable, skipping call", op=op, **log_fields)
                return None
            self._connected = True  # re-probe window elapsed
        try:
            from scalo.http import AsyncHttpClient

            async with AsyncHttpClient(
                base_url=self._base_url, timeout=_TIMEOUT_SECONDS, retries=_RETRIES
            ) as client:
                verb = getattr(client, method)
                kwargs: dict[str, Any] = {"headers": self._headers()}
                if json_body is not None:
                    kwargs["json"] = json_body
                response = await verb(path, **kwargs)
                response.raise_for_status()
                # PUT/DELETE succeed with an empty body; report success, not JSON.
                if not response.content:
                    return {}
                return response.json()
        except Exception as exc:
            self._connected = False
            self._retry_at = time.monotonic() + _RETRY_AFTER_SECONDS
            logger.warning("HyperDX call failed (non-fatal)", op=op, error=str(exc), **log_fields)
            return None

    def _headers(self) -> dict[str, str]:
        """Return authorization headers for HyperDX API calls."""
        # Called per request, so a caching token_provider re-mints before expiry.
        bearer = self._token_provider() if self._token_provider else self._api_key
        return {
            "Authorization": f"Bearer {bearer}",
            "Content-Type": "application/json",
        }
