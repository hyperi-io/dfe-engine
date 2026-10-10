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
create/delete endpoints; the surface this client reaches is::

    GET  /team                    the caller's team (JIT-creates the default team)
    POST /team/invitation         invite an email to the caller's team
    GET/POST /sources             telemetry sources on the caller's team
    PUT/DELETE /sources/:id

A team's ClickHouse CONNECTION is deliberately absent: the fork creates exactly
one per team from the per-org material ``GET /api/v1/hyperdx/connection`` serves
against that user's own token, and an engine-written connection would hand every
team the same credential and stop the fork provisioning the per-org one
(dfe-engine#124).

The one exception to session scope is the fork's ``/dfe/sources`` routes, which
act on every team rather than the caller's: the engine's own team has no
connection and no humans, so a DFE source written there reaches nobody.

All operations are **non-fatal**: if HyperDX is unreachable, failures are
logged as warnings and the caller proceeds normally.

Usage::

    from dfe_engine.hyperdx.client import HyperDXClient

    client = HyperDXClient(
        base_url="http://hyperdx:8080",
        token_provider=machine_token_source.token,
    )
    team = await client.get_team()
    written = await client.put_dfe_source("filebeat", spec)
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from scalo.logger import logger

from dfe_engine.auth.oidc.idp_errors import describe_idp_error

# This is a non-fatal side channel called inside interactive API requests
# (org create), so an unreachable fork must cost seconds, not the
# AsyncHttpClient default of 30s x 3 retries.
_TIMEOUT_SECONDS = 5.0
_RETRIES = 1
# After a failure the breaker re-probes once this window elapses, so a fork
# that boots after the engine recovers without an engine restart.
_RETRY_AFTER_SECONDS = 60.0


class HyperDXClient:
    """Manage the fork's session-scoped team and sources.

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
    # DFE sources (the fork's cross-team fan-out, not the team-scoped surface)
    # ------------------------------------------------------------------

    async def put_dfe_source(self, name: str, spec: dict[str, Any]) -> dict[str, Any] | None:
        """Create or replace one DFE source on EVERY team, each over its own connection.

        Args:
            name: DFE source name; the HyperDX source carries the same name.
            spec: Source body without ``name`` or ``connection``.

        Returns:
            ``{"name", "written", "skipped"}`` by team name, or None on failure.
        """
        data = await self._request(
            "put",
            f"/dfe/sources/{name}",
            json_body=spec,
            op="put_dfe_source",
            source=name,
        )
        return data if isinstance(data, dict) else None

    async def delete_dfe_source(self, name: str) -> dict[str, Any] | None:
        """Remove one DFE source from every team holding it.

        Returns:
            ``{"name", "removed"}`` by team name, or None on failure.
        """
        data = await self._request(
            "delete",
            f"/dfe/sources/{name}",
            op="delete_dfe_source",
            source=name,
        )
        return data if isinstance(data, dict) else None

    async def list_dfe_sources(self) -> dict[str, Any] | None:
        """List every team and the DFE sources it holds, or None on failure."""
        data = await self._request("get", "/dfe/sources", op="list_dfe_sources")
        return data if isinstance(data, dict) else None

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
            # The exception text names the request URL, so the log carries the class and the status.
            logger.warning(
                "HyperDX call failed (non-fatal)", op=op, **describe_idp_error(exc), **log_fields
            )
            return None

    def _headers(self) -> dict[str, str]:
        """Return authorization headers for HyperDX API calls."""
        # Called per request, so a caching token_provider re-mints before expiry.
        bearer = self._token_provider() if self._token_provider else self._api_key
        return {
            "Authorization": f"Bearer {bearer}",
            "Content-Type": "application/json",
        }
