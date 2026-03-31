#  Project:      dfe-engine
#  File:         api/v1/orgs.py
#  Purpose:      Org CRUD REST endpoints (admin for write, read for list/get)
#  Language:     Python
#
#  License:      FSL-1.1-ALv2
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Organisation management router — CRUD for customer organisations.

POST   /api/v1/orgs                → Create org
GET    /api/v1/orgs                → List orgs
GET    /api/v1/orgs/{name}         → Get org
PUT    /api/v1/orgs/{name}         → Update org
DELETE /api/v1/orgs/{name}         → Delete org

Write endpoints require admin role (org:write).
Read endpoints require org:read.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request
from hyperi_pylib.logger import logger
from pydantic import BaseModel, Field

from dfe_engine.api.deps import CurrentUser, require_action

router = APIRouter(prefix="/orgs", tags=["Organisations"])


# -- Request / Response models -----------------------------------------------


class CreateOrgRequest(BaseModel):
    name: str = Field(description="Unique org name (used as identifier)")
    display_name: str = Field(default="", description="Human-readable label")
    org_ids: list[str] = Field(
        default_factory=list,
        description="Tenant IDs for ClickHouse row-level security",
    )


class UpdateOrgRequest(BaseModel):
    display_name: str | None = Field(None, description="Human-readable label")
    org_ids: list[str] | None = Field(None, description="Tenant IDs")
    enabled: bool | None = Field(None, description="Enable or disable the org")


class OrgResponse(BaseModel):
    name: str
    display_name: str
    org_ids: list[str]
    enabled: bool
    created_at: str
    updated_at: str


# -- Endpoints ---------------------------------------------------------------


@router.post(
    "",
    response_model=OrgResponse,
    status_code=201,
    dependencies=[Depends(require_action("org:write"))],
)
async def create_org(
    body: CreateOrgRequest,
    user: CurrentUser,
    request: Request,
):
    """Create a new customer organisation (admin only).

    If HyperDX integration is enabled, fires a background task to
    create the HyperDX team.  Failure is non-fatal.
    """
    from dfe_engine.orgs.registry import OrgRegistry

    registry: OrgRegistry = request.app.state.org_registry
    if registry.get(body.name) is not None:
        raise HTTPException(
            status_code=409,
            detail={"code": "conflict", "message": f"Org '{body.name}' already exists"},
        )

    org = registry.create(body.name, org_ids=body.org_ids, display_name=body.display_name)

    # Fire-and-forget HyperDX team creation (store ref to prevent GC)
    hdx_client = getattr(request.app.state, "hyperdx_client", None)
    if hdx_client is not None:
        background_tasks = getattr(request.app.state, "_bg_tasks", set())
        task = asyncio.create_task(_create_hyperdx_team(hdx_client, org.name))
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)
        request.app.state._bg_tasks = background_tasks

    return OrgResponse(
        name=org.name,
        display_name=org.display_name,
        org_ids=org.org_ids,
        enabled=org.enabled,
        created_at=org.created_at,
        updated_at=org.updated_at,
    )


@router.get(
    "",
    response_model=list[OrgResponse],
    dependencies=[Depends(require_action("org:read"))],
)
async def list_orgs(
    user: CurrentUser,
    request: Request,
):
    """List all organisations."""
    from dfe_engine.orgs.registry import OrgRegistry

    registry: OrgRegistry = request.app.state.org_registry
    return [
        OrgResponse(
            name=o.name,
            display_name=o.display_name,
            org_ids=o.org_ids,
            enabled=o.enabled,
            created_at=o.created_at,
            updated_at=o.updated_at,
        )
        for o in registry.list()
    ]


@router.get(
    "/{name}",
    response_model=OrgResponse,
    dependencies=[Depends(require_action("org:read"))],
)
async def get_org(
    name: str,
    user: CurrentUser,
    request: Request,
):
    """Get a single organisation by name."""
    from dfe_engine.orgs.registry import OrgRegistry

    registry: OrgRegistry = request.app.state.org_registry
    org = registry.get(name)
    if org is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Org '{name}' not found"},
        )
    return OrgResponse(
        name=org.name,
        display_name=org.display_name,
        org_ids=org.org_ids,
        enabled=org.enabled,
        created_at=org.created_at,
        updated_at=org.updated_at,
    )


@router.put(
    "/{name}",
    response_model=OrgResponse,
    dependencies=[Depends(require_action("org:write"))],
)
async def update_org(
    name: str,
    body: UpdateOrgRequest,
    user: CurrentUser,
    request: Request,
):
    """Update an organisation (admin only)."""
    from dfe_engine.orgs.registry import OrgRegistry

    registry: OrgRegistry = request.app.state.org_registry
    if registry.get(name) is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Org '{name}' not found"},
        )
    update_fields: dict[str, object] = {}
    if body.display_name is not None:
        update_fields["display_name"] = body.display_name
    if body.org_ids is not None:
        update_fields["org_ids"] = body.org_ids
    if body.enabled is not None:
        update_fields["enabled"] = body.enabled

    org = registry.update(name, **update_fields)
    return OrgResponse(
        name=org.name,
        display_name=org.display_name,
        org_ids=org.org_ids,
        enabled=org.enabled,
        created_at=org.created_at,
        updated_at=org.updated_at,
    )


@router.delete(
    "/{name}",
    status_code=204,
    dependencies=[Depends(require_action("org:write"))],
)
async def delete_org(
    name: str,
    user: CurrentUser,
    request: Request,
):
    """Delete an organisation (admin only)."""
    from dfe_engine.orgs.registry import OrgRegistry

    registry: OrgRegistry = request.app.state.org_registry
    if registry.get(name) is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Org '{name}' not found"},
        )
    registry.delete(name)


# -- Background helpers ------------------------------------------------------


async def _create_hyperdx_team(hdx_client: object, org_name: str) -> None:
    """Fire-and-forget HyperDX team creation.  Logs warning on failure."""
    try:
        from dfe_engine.hyperdx.client import HyperDXClient

        if isinstance(hdx_client, HyperDXClient):
            await hdx_client.create_team(f"customer-{org_name}")
    except Exception as exc:
        logger.warning(
            "Background HyperDX team creation failed (non-fatal)",
            org=org_name,
            error=str(exc),
        )
