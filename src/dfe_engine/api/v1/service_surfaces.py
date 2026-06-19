#  Project:      dfe-engine
#  File:         api/v1/service_surfaces.py
#  Purpose:      REST endpoints for schema-less Rust service surface discovery
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Service surfaces router — discover Rust service config + metrics.

GET  /api/v1/service-surfaces                          → List all surfaces
GET  /api/v1/service-surfaces/{name}                   → Get surface detail
POST /api/v1/service-surfaces/{name}/metrics/refresh   → Re-fetch manifest
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from dfe_engine.api.deps import CurrentUser, require_action
from dfe_engine.auth.rbac_scopes import scopes_dict
from dfe_engine.services.surfaces.models import ServiceSurface
from dfe_engine.services.surfaces.registry import SurfaceRegistry

router = APIRouter(prefix="/service-surfaces", tags=["Service Surfaces"])


# ── Dependencies ────────────────────────────────────────────


def get_surface_registry(request: Request) -> SurfaceRegistry:
    """Resolve SurfaceRegistry from app state."""
    reg = getattr(request.app.state, "surface_registry", None)
    if reg is None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "not_configured",
                "message": "SurfaceRegistry not initialized — set DFE_CONFIG_DIR",
            },
        )
    return reg


SurfaceReg = Annotated[SurfaceRegistry, Depends(get_surface_registry)]


# ── Response models ─────────────────────────────────────────


class SurfaceSummary(BaseModel):
    """Compact summary for list endpoint."""

    service: str
    description: str = ""
    config_count: int = 0
    metrics_count: int = 0
    manifest_url: str = ""


class ManifestRefreshResponse(BaseModel):
    """Result of a manifest refresh attempt."""

    service: str
    refreshed: bool
    discovered_at: str = ""
    metrics_count: int = 0


# ── Endpoints ───────────────────────────────────────────────


@router.get(
    "",
    response_model=list[SurfaceSummary],
    dependencies=[Depends(require_action(scopes_dict["service_surface_read"]))],
)
async def list_service_surfaces(
    user: CurrentUser,
    registry: SurfaceReg,
) -> list[SurfaceSummary]:
    """List all known service surfaces."""
    surfaces = registry.list()
    return [
        SurfaceSummary(
            service=s.service,
            description=s.description,
            config_count=len(s.config_surface),
            metrics_count=len(s.metrics_surface),
            manifest_url=s.manifest_url,
        )
        for s in surfaces
    ]


@router.get(
    "/{name}",
    response_model=ServiceSurface,
    dependencies=[Depends(require_action(scopes_dict["service_surface_read"]))],
)
async def get_service_surface(
    name: str,
    user: CurrentUser,
    registry: SurfaceReg,
) -> ServiceSurface:
    """Get the full surface definition for a single service."""
    surface = registry.get(name)
    if surface is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "not_found",
                "message": f"Service surface '{name}' not found",
            },
        )
    return surface


@router.post(
    "/{name}/metrics/refresh",
    response_model=ManifestRefreshResponse,
    dependencies=[Depends(require_action(scopes_dict["service_surface_write"]))],
)
async def refresh_service_manifest(
    name: str,
    user: CurrentUser,
    registry: SurfaceReg,
) -> ManifestRefreshResponse:
    """Re-fetch the metrics manifest from a running service.

    Contacts the service's ``manifest_url`` to update its metrics_surface.
    Returns the refresh result.  Does not fail if the service is unreachable
    — returns ``refreshed=false`` instead.
    """
    surface = registry.get(name)
    if surface is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "not_found",
                "message": f"Service surface '{name}' not found",
            },
        )

    updated = await registry.refresh_manifest(name)
    if updated is None:
        return ManifestRefreshResponse(service=name, refreshed=False)

    return ManifestRefreshResponse(
        service=name,
        refreshed=bool(updated.discovered_at),
        discovered_at=updated.discovered_at,
        metrics_count=len(updated.metrics_surface),
    )
