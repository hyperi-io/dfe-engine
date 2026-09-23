#  Project:      dfe-engine
#  File:         surfaces/registry.py
#  Purpose:      Registry for service surface definitions loaded from YAML
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""SurfaceRegistry — loads service surface YAML files and provides CRUD.

Each YAML file in the surfaces directory describes one Rust service's
configurable settings and metrics.  Adding a new dfe-* service requires
adding a YAML file — zero Python code.

Built-in surface definitions are seeded from package resources on first
load when the target directory is empty.
"""

from __future__ import annotations

import builtins
import importlib.resources
from pathlib import Path

from scalo.logger import logger

from dfe_engine.services.surfaces.models import ServiceSurface
from dfe_engine.yaml_utils import yaml_dump, yaml_load


class SurfaceNotFoundError(Exception):
    """Raised when a service surface is not found."""


class SurfaceRegistry:
    """Registry of service surface definitions, loaded from YAML files.

    Each file in the surfaces directory describes one Rust service's
    configurable settings and metrics.
    """

    def __init__(self, surfaces_dir: Path) -> None:
        self._dir = Path(surfaces_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._seed_if_empty()

    # ── Read operations ──────────────────────────────────────

    def get(self, service_name: str) -> ServiceSurface | None:
        """Get a service surface by name.  Returns None if not found."""
        path = self._path_for(service_name)
        if not path.exists():
            return None
        return self._load_surface(path)

    def list(self) -> builtins.list[ServiceSurface]:
        """List all service surfaces sorted by service name."""
        surfaces: list[ServiceSurface] = []
        for path in sorted(self._dir.glob("*.yaml")):
            surface = self._load_surface(path)
            if surface is not None:
                surfaces.append(surface)
        return surfaces

    # ── Write operations ─────────────────────────────────────

    def create(self, surface: ServiceSurface) -> None:
        """Write a new surface YAML file.

        Raises:
            FileExistsError: If a surface for this service already exists.
        """
        path = self._path_for(surface.service)
        if path.exists():
            raise FileExistsError(f"Surface already exists: {surface.service}")
        self._write_surface(path, surface)
        logger.info("Created service surface", service=surface.service)

    def update(self, service_name: str, surface: ServiceSurface) -> None:
        """Update an existing surface YAML file.

        Raises:
            SurfaceNotFoundError: If the surface does not exist.
        """
        path = self._path_for(service_name)
        if not path.exists():
            raise SurfaceNotFoundError(f"Surface not found: {service_name}")
        # If the service name changed, remove the old file
        if surface.service != service_name:
            path.unlink()
            path = self._path_for(surface.service)
        self._write_surface(path, surface)
        logger.info("Updated service surface", service=surface.service)

    def delete(self, service_name: str) -> None:
        """Delete a service surface YAML file.

        Raises:
            SurfaceNotFoundError: If the surface does not exist.
        """
        path = self._path_for(service_name)
        if not path.exists():
            raise SurfaceNotFoundError(f"Surface not found: {service_name}")
        path.unlink()
        logger.info("Deleted service surface", service=service_name)

    # ── Manifest refresh ─────────────────────────────────────

    async def refresh_manifest(self, service_name: str) -> ServiceSurface | None:
        """Fetch /metrics/manifest from the service and update the surface.

        Returns None if the service is unreachable or has no manifest_url.
        This is a placeholder for Phase 1.5 of scalo-rs — when scalo-rs
        exposes a /metrics/manifest endpoint, this method will fetch it
        and update the metrics_surface.
        """
        from datetime import UTC, datetime

        surface = self.get(service_name)
        if surface is None:
            return None
        if not surface.manifest_url:
            logger.debug(
                "No manifest_url configured",
                service=service_name,
            )
            return surface

        try:
            from scalo.http import AsyncHttpClient

            async with AsyncHttpClient() as client:
                response = await client.get(surface.manifest_url)
                if response.status_code != 200:
                    logger.warning(
                        "Manifest fetch failed",
                        service=service_name,
                        status=response.status_code,
                    )
                    return surface
                manifest = response.json()
        except Exception as e:
            logger.warning(
                "Manifest fetch error",
                service=service_name,
                error=str(e),
            )
            return surface

        # Update metrics_surface from manifest if it has a metrics list
        from dfe_engine.services.surfaces.models import MetricEntry

        if isinstance(manifest, dict) and "metrics" in manifest:
            metrics = []
            for m in manifest["metrics"]:
                if isinstance(m, dict) and "name" in m:
                    metrics.append(MetricEntry(**m))
            surface.metrics_surface = metrics

        surface.discovered_at = datetime.now(UTC).isoformat()
        self._write_surface(self._path_for(service_name), surface)
        logger.info("Refreshed manifest", service=service_name)
        return surface

    # ── Internal helpers ─────────────────────────────────────

    def _path_for(self, service_name: str) -> Path:
        """Return the YAML file path for a service name."""
        return self._dir / f"{service_name}.yaml"

    def _load_surface(self, path: Path) -> ServiceSurface | None:
        """Load and validate a surface YAML file."""
        try:
            data = yaml_load(path)
            if not isinstance(data, dict):
                logger.warning("Invalid surface file (not a dict)", path=str(path))
                return None
            return ServiceSurface(**data)
        except Exception as e:
            logger.warning(
                "Failed to load surface",
                path=str(path),
                error=str(e),
            )
            return None

    def _write_surface(self, path: Path, surface: ServiceSurface) -> None:
        """Serialise a surface to YAML."""
        data = surface.model_dump(mode="json")
        yaml_dump(data, path)

    def _seed_if_empty(self) -> None:
        """Seed built-in surface files if the directory has none."""
        existing = list(self._dir.glob("*.yaml"))
        if existing:
            return

        pkg = importlib.resources.files("dfe_engine.services.surfaces.resources")
        count = 0
        for resource in pkg.iterdir():
            if resource.name.endswith(".yaml"):
                with importlib.resources.as_file(resource) as src:
                    dest = self._dir / resource.name
                    dest.write_text(src.read_text())
                    count += 1

        if count > 0:
            logger.info("Seeded service surfaces from built-ins", count=count)
