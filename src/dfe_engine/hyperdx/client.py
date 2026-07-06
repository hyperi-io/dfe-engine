#  Project:      dfe-engine
#  File:         hyperdx/client.py
#  Purpose:      HyperDX internal API client for team and connection management
#  Language:     Python
#
#  License:      BUSL-1.1
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
from typing import Any

from scalo.logger import logger

from dfe_engine.connections.config import ConnectionConfig
from dfe_engine.connections.models import ClickHouseConnection
from dfe_engine.governance.ch import TENANT_READER_USER, TENANT_SETTING

# Deploy-repo path where the per-org DEFAULT_CONNECTIONS JSON lands. The HyperDX
# fork chart consumes this file (raw JSON array) as its DEFAULT_CONNECTIONS env so
# each org's ClickHouse connection is seeded declaratively via git. NOTE for
# fork/infra: wire this file into the HyperDX app's DEFAULT_CONNECTIONS value.
HYPERDX_CONNECTIONS_PATH = "hyperdx/connections.json"

# Deploy-repo path where the DEFAULT_SOURCES JSON lands (Task F). One HyperDX
# `log` source per built source table so dashboards can query the new table the
# moment its schema is deployed. Same consumption model as the connections file:
# the fork chart wires it into the HyperDX app's DEFAULT_SOURCES env.
HYPERDX_SOURCES_PATH = "hyperdx/sources.json"


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
        response = await self._request(
            "post",
            "/api/v1/teams",
            json={"name": name},
            op="create_team",
            context={"team": name},
        )
        if response is None:
            return None
        team_id = response.json().get("id", "")
        logger.info("HyperDX team created", team=name, team_id=team_id)
        return team_id

    async def create_connection(
        self,
        team_id: str,
        name: str,
        host: str,
        port: int,
        database: str,
        user: str,
        password: str,
        settings: dict[str, str] | None = None,
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
            settings: Optional per-connection ClickHouse custom settings (e.g.
                ``{"DFE_current_tenant_id": "acme,globex"}``). Emitted under
                ``clickhouseSettings`` - the same clearly-named key the
                DEFAULT_CONNECTIONS artifact uses. See ``build_hyperdx_connections_json``
                for the fork-dependency note (the fork must merge this into each query).

        Returns:
            Connection ID string on success, None on failure.
        """
        body: dict[str, Any] = {
            "name": name,
            "host": host,
            "port": port,
            "database": database,
            "user": user,
            "password": password,
        }
        if settings:
            body["clickhouseSettings"] = settings
        response = await self._request(
            "post",
            f"/api/v1/teams/{team_id}/connections",
            json=body,
            op="create_connection",
            context={"team_id": team_id, "connection": name},
        )
        if response is None:
            return None
        conn_id = response.json().get("id", "")
        logger.info(
            "HyperDX connection created",
            team_id=team_id,
            connection=name,
            conn_id=conn_id,
        )
        return conn_id

    async def delete_connection(self, team_id: str, conn_id: str) -> bool:
        """Delete a ClickHouse connection from a HyperDX team.

        Args:
            team_id: HyperDX team ID.
            conn_id: Connection ID to delete.

        Returns:
            True on success, False on failure.
        """
        response = await self._request(
            "delete",
            f"/api/v1/teams/{team_id}/connections/{conn_id}",
            op="delete_connection",
            context={"team_id": team_id, "conn_id": conn_id},
        )
        if response is None:
            return False
        logger.info("HyperDX connection deleted", team_id=team_id, conn_id=conn_id)
        return True

    async def delete_team(self, team_id: str) -> bool:
        """Delete a HyperDX team.

        Args:
            team_id: HyperDX team ID to delete.

        Returns:
            True on success, False on failure.
        """
        response = await self._request(
            "delete",
            f"/api/v1/teams/{team_id}",
            op="delete_team",
            context={"team_id": team_id},
        )
        if response is None:
            return False
        logger.info("HyperDX team deleted", team_id=team_id)
        return True

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
        response = await self._request(
            "put",
            f"/api/v1/teams/{team_id}/connections/{connection_id}",
            json=dict(kwargs),
            op="update_connection",
            context={"team_id": team_id, "connection_id": connection_id},
        )
        if response is None:
            return False
        logger.info(
            "HyperDX connection updated",
            team_id=team_id,
            connection_id=connection_id,
        )
        return True

    async def invite_member(self, team_api_key: str, email: str) -> bool:
        """Invite a user to a HyperDX team by email.

        Uses the team's own API key (not the admin key) since HyperDX team
        endpoints are team-scoped.  A per-team failure does NOT mark the whole
        client unreachable (``mark_disconnected=False``).

        Args:
            team_api_key: API key for the target team.
            email: Email address to invite.

        Returns:
            True on success, False on failure (non-fatal).
        """
        response = await self._request(
            "post",
            "/api/v1/team/invitation",
            json={"email": email},
            headers=self._bearer(team_api_key),
            op="invite_member",
            context={"email": email},
            mark_disconnected=False,
        )
        if response is None:
            return False
        logger.info("HyperDX member invited", email=email)
        return True

    async def remove_member(self, team_api_key: str, email: str) -> bool:
        """Remove a user from a HyperDX team by email.

        Propagation counterpart to ``invite_member``: when an account is
        disabled/deleted or a user is removed from an org's group, the user's
        team membership must be revoked so the org's HyperDX no longer lets them
        in. Team-scoped (uses the team's own API key), and a per-team failure
        does NOT mark the whole client unreachable (``mark_disconnected=False``).

        NOTE for fork/infra: the revoke endpoint is ``DELETE
        /api/v1/team/members/{email}`` - confirm against the fork's team API
        (mirror of the ``/api/v1/team/invitation`` invite path).

        Args:
            team_api_key: API key for the target team.
            email: Email address to remove.

        Returns:
            True on success, False on failure (non-fatal).
        """
        response = await self._request(
            "delete",
            f"/api/v1/team/members/{email}",
            headers=self._bearer(team_api_key),
            op="remove_member",
            context={"email": email},
            mark_disconnected=False,
        )
        if response is None:
            return False
        logger.info("HyperDX member removed", email=email)
        return True

    async def get_team_api_key(self, team_id: str) -> str | None:
        """Get the API key for a specific team.

        Uses the admin API key to retrieve team info.

        Args:
            team_id: HyperDX team ID.

        Returns:
            Team API key string, or None on failure.
        """
        response = await self._request(
            "get",
            f"/api/v1/teams/{team_id}",
            op="get_team_api_key",
            context={"team_id": team_id},
        )
        if response is None:
            return None
        data = response.json()
        return data.get("apiKey") or data.get("api_key") or None

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
        connections = [
            {
                "name": name,
                "host": conn.host,
                "port": conn.port,
                "database": conn.database,
                "user": conn.user,
                "password": os.environ.get(conn.password_env, ""),
            }
            for name, conn in conn_config.connections.items()
        ]
        return json.dumps(connections)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        op: str,
        context: dict[str, Any],
        mark_disconnected: bool = True,
    ):
        """Shared HyperDX call: guard, request, ``raise_for_status``, swallow.

        Collapses the per-method non-fatal HTTP block (the ``_connected`` guard,
        the ``AsyncHttpClient`` async-with, ``raise_for_status``, and the
        except-log-return path) into one place. Callers supply only the verb,
        URL, payload, and the log ``op``/``context``; each maps the returned
        Response (body/status) to its own result, or the ``None`` sentinel to its
        own default.

        ``mark_disconnected`` gates the ``_connected = False`` latch: the
        admin-key methods trip it (one HyperDX outage short-circuits the rest),
        but the team-scoped ``invite_member`` / ``remove_member`` do NOT - a
        single team's API-key failure must not disable the whole client.

        Returns the ``Response`` on success, or ``None`` on the disconnected
        guard or any failure.
        """
        if not self._connected:
            logger.warning(f"HyperDX unreachable, skipping {op}", **context)
            return None
        try:
            from scalo.http import AsyncHttpClient

            request_kwargs: dict[str, Any] = {"headers": headers or self._headers()}
            if json is not None:
                request_kwargs["json"] = json
            async with AsyncHttpClient(base_url=self._base_url) as client:
                response = await getattr(client, method)(path, **request_kwargs)
                response.raise_for_status()
                return response
        except Exception as exc:
            if mark_disconnected:
                self._connected = False
            logger.warning(f"HyperDX {op} failed (non-fatal)", **context, error=str(exc))
            return None

    def _headers(self) -> dict[str, str]:
        """Return authorization headers for HyperDX API calls (admin key)."""
        return self._bearer(self._api_key)

    @staticmethod
    def _bearer(token: str) -> dict[str, str]:
        """Bearer auth + JSON content-type headers for *token*."""
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }


def build_hyperdx_connections_json(
    orgs: list[Any],
    *,
    base: ClickHouseConnection,
    secrets_store: Any,
) -> str:
    """Build the HyperDX per-ORG DEFAULT_CONNECTIONS JSON under the tenant-reader model.

    ONE ClickHouse connection per enabled org, and every connection authenticates
    as the SAME shared, row-filtered reader ``dfe_tenant_reader`` (secret path
    ``ch/fixed/dfe_tenant_reader``, minted by ``ChRbacReconciler``). Isolation is
    NO LONGER the CH identity - the retired per-group ``dfe_grp_<group>`` users are
    gone. Each connection instead carries a baked-in ``DFE_current_tenant_id``
    custom setting = the org's comma-joined ``org_ids``; the ONE restrictive row
    policy on every ``_org_id`` table (governance.ch.render_tenant_policies) reads
    that setting via ``getSetting`` and scopes the reader to exactly those orgs. An
    empty setting fails CLOSED (0 rows), so an org with no ``org_ids`` shows nothing
    rather than everything.

    ``base`` supplies the shared network coordinates (host/port/database) - every
    org connects to the SAME ClickHouse as the SAME user; only the tenant setting
    differs. Pure apart from the single secrets read for the shared reader password;
    no HTTP. A missing reader secret yields an empty password (non-fatal) so the map
    stays structurally complete and the password lands on the next publish once the
    reconciler has minted it.

    FORK DEPENDENCY: the HyperDX fork Connection schema carries no generic per-query
    settings map today - only ``hyperdxSettingPrefix``, which builds ``<prefix>_user``
    from the session email (clickhouseProxy.ts), not a static tenant id. The tenant
    setting is therefore emitted under a clearly-named ``clickhouseSettings`` object
    (mirroring the fork CH client's own ``clickhouse_settings`` query param); the fork
    must merge that map into every query it proxies for the connection. Unknown keys
    are stripped by the fork's zod/Mongoose parse today, so emitting it now is
    forward-safe (inert until the fork wires it).
    """
    password = _read_fixed_reader_secret(secrets_store)
    connections = [
        {
            "name": org.name,
            "host": base.host,
            "port": base.port,
            "database": base.database,
            "user": TENANT_READER_USER,
            "password": password,
            "clickhouseSettings": {TENANT_SETTING: _tenant_value(org)},
        }
        for org in orgs
        if getattr(org, "enabled", True)
    ]
    return json.dumps(connections)


def _tenant_value(org: Any) -> str:
    """Comma-joined org_ids for the org's DFE_current_tenant_id setting ('' -> 0 rows)."""
    return ",".join(getattr(org, "org_ids", None) or [])


def _read_fixed_reader_secret(secrets_store: Any) -> str:
    """Read the shared dfe_tenant_reader plaintext from the seam; '' if absent (non-fatal).

    Path ``ch/fixed/dfe_tenant_reader`` mirrors where ``ChRbacReconciler`` stores the
    minted fixed-user plaintext (``ch/fixed/<name>``).
    """
    if secrets_store is None:
        return ""
    try:
        return secrets_store.get(f"ch/fixed/{TENANT_READER_USER}")
    except Exception:
        return ""


def build_hyperdx_sources_json(
    source_tables: list[tuple[str, str]],
    *,
    connection: str,
) -> str:
    """Build the HyperDX DEFAULT_SOURCES JSON (Task F): one `log` source per table.

    ``source_tables`` is a list of ``(db, table)`` pairs for the built/deployed
    source tables. Each yields one idempotent HyperDX ``log`` source keyed by name
    (``<db>.<table>``), so re-emitting an unchanged set produces byte-identical JSON
    and the gitops publish is a no-op (declarative + git-only, the survivability
    contract). The DFE landing schema is fixed, so the expressions are constant:
    ``_timestamp_load`` is the event time, ``_json`` the raw body.

    FORK DEPENDENCY: ``connection`` must match a DEFAULT_CONNECTIONS entry name.
    Under the tenant-reader model there is one connection PER ORG, but a source table
    is org-agnostic (isolation is the connection's tenant setting, not the source),
    so a single logical source cannot name every per-org connection. Emit against one
    named connection here (the caller passes it); wiring the source list across the
    per-org connections (fan-out, or a fork source->connection selector) is the fork
    side. See setupDefaults.ts (sources reference a connection by name).
    """
    sources = [
        {
            "name": f"{db}.{table}",
            "kind": "log",
            "connection": connection,
            "from": {"databaseName": db, "tableName": table},
            "timestampValueExpression": "_timestamp_load",
            "defaultTableSelectExpression": "_timestamp_load, _json",
            "bodyExpression": "_json",
        }
        for db, table in source_tables
    ]
    return json.dumps(sources)
