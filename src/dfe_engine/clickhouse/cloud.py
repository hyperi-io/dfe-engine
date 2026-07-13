#  Project:      dfe-engine
#  File:         clickhouse/cloud.py
#  Purpose:      ClickHouse Cloud service lifecycle over the management API
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""ClickHouse Cloud service lifecycle (CONTROL PLANE) via the management API.

Status / start / stop / wait-running for a CH Cloud service through
``api.clickhouse.cloud``, using scalo's :class:`~scalo.http.HttpClient` (Stamina
back-off, observability) - NOT urllib/httpx directly (pylib policy). This is the
CONTROL plane and a BILLABLE lever: it is SEPARATE from the SQL data-plane
connection (the regular ``clickhouse.*`` block pointed at a ``*.clickhouse.cloud``
host). Wraps the "EV start button for a dev CH Cloud service": see it, start it,
stop it - manually via the API/CLI, or opt-in auto-wake (Phase 2).

Control-plane permissions the api key needs (least-privilege):
  - GET  /v1/organizations                              (discover the org)
  - GET  /v1/organizations/{org}/services               (list + read state)
  - PATCH /v1/organizations/{org}/services/{id}/state   (start / stop)
A SERVICE-SCOPED key with service read + state-management is sufficient (NOT an
org-admin key; no billing / member-management). Every start it issues is billable
and is logged.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from scalo.logger import logger

from ..settings import ClickHouseCloudSettings

# CH Cloud service states we key behaviour on. "stopped" needs an explicit start
# (slow cold-start); "idle" auto-wakes on connect but also accepts a start; the
# transitional states just mean "keep polling".
_RUNNING = "running"
_STOPPED_STATES = frozenset({"stopped", "idle"})
_TRANSITIONING = frozenset({"starting", "stopping", "provisioning", "awaking", "degraded"})


class CloudServiceError(Exception):
    """A ClickHouse Cloud management-API call failed."""


@dataclass(slots=True)
class CloudServiceStatus:
    """A CH Cloud service's control-plane state."""

    id: str
    name: str
    state: str

    @property
    def is_running(self) -> bool:
        return self.state == _RUNNING

    @property
    def is_stopped(self) -> bool:
        return self.state in _STOPPED_STATES

    @property
    def is_transitioning(self) -> bool:
        return self.state in _TRANSITIONING


class CloudService:
    """Drive a ClickHouse Cloud service's lifecycle over the management API.

    Args:
        config: the ``clickhouse.cloud`` settings block (control-plane creds +
            service selector).
        http_factory: OPTIONAL () -> an HttpClient-like context manager exposing
            ``get(path)`` / ``patch(path, json=...)`` returning an object with
            ``.json()``. Defaults to a scalo :class:`HttpClient` with basic auth.
            Tests inject a fake so no network is touched.
        sleep / now: injectable clock for :meth:`wait_running` (tests pass fakes
            so they never really sleep and never race a wall clock).
    """

    def __init__(
        self,
        config: ClickHouseCloudSettings,
        *,
        http_factory: Callable[[], Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._http_factory = http_factory or self._default_http_factory
        self._sleep = sleep
        self._now = now
        self._org_id_cache = config.organization_id or ""

    def _default_http_factory(self) -> Any:
        # Lazy import (pylib convention) so importing this module never drags httpx.
        from scalo.http import HttpClient

        if not self._config.configured:
            raise CloudServiceError(
                "ClickHouse Cloud is not configured: set DFE_CLICKHOUSE_CLOUD_API_KEY_ID + "
                "DFE_CLICKHOUSE_CLOUD_API_KEY_SECRET (the control-plane key)."
            )
        return HttpClient(
            base_url=self._config.api_base,
            timeout=30.0,
            retries=3,
            auth=(self._config.api_key_id, self._config.api_key_secret),
        )

    def _get(self, path: str) -> dict:
        try:
            with self._http_factory() as client:
                return client.get(path).json()
        except CloudServiceError:
            raise
        except Exception as exc:
            raise CloudServiceError(f"GET {path} failed: {exc}") from exc

    def _patch(self, path: str, body: dict) -> dict:
        try:
            with self._http_factory() as client:
                return client.patch(path, json=body).json()
        except CloudServiceError:
            raise
        except Exception as exc:
            raise CloudServiceError(f"PATCH {path} failed: {exc}") from exc

    def _org_id(self) -> str:
        """The org id - configured, or auto-discovered from the key (cached)."""
        if self._org_id_cache:
            return self._org_id_cache
        result = self._get("/organizations").get("result") or []
        if not result:
            raise CloudServiceError("no ClickHouse Cloud organizations visible to this key")
        self._org_id_cache = result[0]["id"]
        return self._org_id_cache

    def _resolve(self) -> tuple[str, CloudServiceStatus]:
        """Return (org_id, status) for the selected service (by id, else by name)."""
        org_id = self._org_id()
        services = self._get(f"/organizations/{org_id}/services").get("result") or []
        for svc in services:
            if (self._config.service_id and svc.get("id") == self._config.service_id) or (
                not self._config.service_id and svc.get("name") == self._config.service_name
            ):
                return org_id, CloudServiceStatus(
                    id=svc.get("id", ""), name=svc.get("name", ""), state=svc.get("state", "")
                )
        selector = self._config.service_id or self._config.service_name
        available = ", ".join(f"{s.get('name')} ({s.get('state')})" for s in services)
        raise CloudServiceError(f"CH Cloud service {selector!r} not found. Available: {available}")

    # -- Public lifecycle --------------------------------------------------

    def status(self) -> CloudServiceStatus:
        """Read the selected service's current control-plane state."""
        _, status = self._resolve()
        return status

    def start(self) -> CloudServiceStatus:
        """Start (wake) the service if it is stopped/idle. Idempotent + billable.

        Returns the post-issue status. A running/starting service is a no-op. Every
        start is logged - it is a billable action.
        """
        org_id, status = self._resolve()
        if status.is_running or status.state == "starting":
            return status
        logger.info(
            "Starting ClickHouse Cloud service (billable)",
            service=status.name,
            service_id=status.id,
            from_state=status.state,
        )
        self._patch(f"/organizations/{org_id}/services/{status.id}/state", {"command": "start"})
        return self.status()

    def stop(self) -> CloudServiceStatus:
        """Stop the service if it is running/idle. Idempotent (saves cost)."""
        org_id, status = self._resolve()
        if status.state in ("stopped", "stopping"):
            return status
        logger.info(
            "Stopping ClickHouse Cloud service",
            service=status.name,
            service_id=status.id,
            from_state=status.state,
        )
        self._patch(f"/organizations/{org_id}/services/{status.id}/state", {"command": "stop"})
        return self.status()

    def wait_running(self, *, timeout: float, poll_interval: float = 10.0) -> CloudServiceStatus:
        """Poll until the service is ``running`` or *timeout* (seconds) elapses.

        Gate on the real ``state==running`` signal (not a timer race); poll every
        ``poll_interval`` and return the INSTANT it is up (fast reconnect); fail
        only when the GENEROUS *timeout* budget is exceeded. Both are config-cascade
        values from the caller, never hardcoded here.
        """
        deadline = self._now() + timeout
        while True:
            status = self.status()
            if status.is_running:
                return status
            if self._now() >= deadline:
                raise CloudServiceError(
                    f"CH Cloud service {status.name!r} not running after {timeout:.0f}s "
                    f"(state={status.state})"
                )
            self._sleep(poll_interval)
