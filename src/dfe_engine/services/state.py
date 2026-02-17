"""Service state client for querying running DFE Rust services.

Provides read-only access to health endpoints and Prometheus metrics
exposed by dfe-receiver, dfe-loader, and dfe-archiver.
"""

from __future__ import annotations

import re
from typing import Any

import httpx
from pydantic import BaseModel

from hyperi_pylib.logger import logger


class HealthStatus(BaseModel):
    """Health status of a running service."""

    alive: bool
    ready: bool
    details: dict[str, Any] = {}


class ServiceStatus(BaseModel):
    """Composite status combining health and key metrics."""

    service: str
    instance: str
    alive: bool
    ready: bool
    health: HealthStatus
    metrics: dict[str, float] = {}
    config_version: int | None = None


# Prometheus text format line pattern: metric_name{labels} value
_PROM_LINE_RE = re.compile(
    r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{[^}]*\})?\s+([\d.eE+-]+(?:nan|inf)?)"
)


def _parse_prometheus_text(text: str) -> dict[str, float]:
    """Parse Prometheus text exposition format into a flat dict.

    Only parses metric values, ignoring labels (takes last value per metric name).
    """
    metrics: dict[str, float] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = _PROM_LINE_RE.match(line)
        if match:
            name = match.group(1)
            try:
                metrics[name] = float(match.group(2))
            except ValueError:
                pass
    return metrics


class ServiceStateClient:
    """Read-only client for querying running Rust service health and metrics.

    Service endpoint conventions:
    - Receiver: health on :8080 (/health/live, /health/ready), metrics on :9090
    - Loader: health + metrics on :9090 (/health, /live, /ready, /metrics)
    - Archiver: metrics on :9090 (no health API — infer from metrics scrape)

    Args:
        service: Service name ('receiver', 'loader', 'archiver')
        base_url: Base URL for health endpoints
        metrics_url: URL for Prometheus metrics (defaults to base_url)
        instance: Instance name for status reporting
        timeout: HTTP timeout in seconds
    """

    def __init__(
        self,
        service: str,
        base_url: str,
        metrics_url: str | None = None,
        instance: str = "default",
        timeout: float = 5.0,
    ) -> None:
        self.service = service
        self.base_url = base_url.rstrip("/")
        self.metrics_url = (metrics_url or base_url).rstrip("/")
        self.instance = instance
        self._timeout = timeout

    # -------------------------------------------------------------------------
    # Health
    # -------------------------------------------------------------------------

    async def health(self) -> HealthStatus:
        """Get health status from the service.

        Tries service-specific health endpoints.
        """
        alive = await self.is_alive()
        ready = False
        details: dict[str, Any] = {}

        if alive:
            ready = await self._check_readiness()

            # Try to get detailed health (loader has /health returning JSON)
            if self.service == "loader":
                try:
                    async with httpx.AsyncClient(timeout=self._timeout) as client:
                        resp = await client.get(f"{self.base_url}/health")
                        if resp.status_code == 200:
                            details = resp.json()
                except Exception:
                    pass

        return HealthStatus(alive=alive, ready=ready, details=details)

    async def is_alive(self) -> bool:
        """Check if the service process is alive (liveness probe)."""
        paths = self._liveness_paths()
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            for path in paths:
                try:
                    resp = await client.get(f"{self.base_url}{path}")
                    if resp.status_code == 200:
                        return True
                except httpx.HTTPError:
                    continue
        return False

    async def readiness(self) -> HealthStatus:
        """Check readiness (includes downstream connectivity)."""
        ready = await self._check_readiness()
        return HealthStatus(alive=True, ready=ready)

    async def _check_readiness(self) -> bool:
        """Check readiness endpoint."""
        paths = self._readiness_paths()
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            for path in paths:
                try:
                    resp = await client.get(f"{self.base_url}{path}")
                    return resp.status_code == 200
                except httpx.HTTPError:
                    continue
        return False

    # -------------------------------------------------------------------------
    # Metrics
    # -------------------------------------------------------------------------

    async def metrics_raw(self) -> str:
        """Fetch raw Prometheus metrics text."""
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(f"{self.metrics_url}/metrics")
            resp.raise_for_status()
            return resp.text

    async def metrics(self) -> dict[str, float]:
        """Fetch and parse Prometheus metrics into a dict."""
        try:
            raw = await self.metrics_raw()
            return _parse_prometheus_text(raw)
        except Exception as e:
            logger.warning(f"Failed to fetch metrics from {self.metrics_url}: {e}")
            return {}

    # -------------------------------------------------------------------------
    # Composite
    # -------------------------------------------------------------------------

    async def status(self, config_version: int | None = None) -> ServiceStatus:
        """Get composite service status (health + key metrics).

        Args:
            config_version: Current config version from registry (optional)

        Returns:
            ServiceStatus combining health and metrics
        """
        health = await self.health()
        metrics_data = {}

        if health.alive:
            try:
                metrics_data = await self.metrics()
            except Exception as e:
                logger.debug(f"Metrics fetch failed for {self.service}: {e}")

        return ServiceStatus(
            service=self.service,
            instance=self.instance,
            alive=health.alive,
            ready=health.ready,
            health=health,
            metrics=metrics_data,
            config_version=config_version,
        )

    # -------------------------------------------------------------------------
    # Service-specific endpoint paths
    # -------------------------------------------------------------------------

    def _liveness_paths(self) -> list[str]:
        """Get liveness probe paths for this service type."""
        if self.service == "receiver":
            return ["/health/live"]
        elif self.service == "loader":
            return ["/live", "/health"]
        else:
            # Archiver has no health API — try metrics endpoint as liveness
            return ["/metrics"]

    def _readiness_paths(self) -> list[str]:
        """Get readiness probe paths for this service type."""
        if self.service == "receiver":
            return ["/health/ready"]
        elif self.service == "loader":
            return ["/ready", "/health"]
        else:
            # Archiver: metrics availability implies readiness
            return ["/metrics"]
