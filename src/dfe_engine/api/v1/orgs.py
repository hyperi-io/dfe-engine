#  Project:      dfe-engine
#  File:         api/v1/orgs.py
#  Purpose:      Org CRUD REST endpoints (admin for write, read for list/get)
#  Language:     Python
#
#  License:      BUSL-1.1
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

from fastapi import APIRouter, Depends, HTTPException, Request
from hyperi_pylib.logger import logger
from pydantic import BaseModel, Field

from dfe_engine.api.deps import CurrentUser, require_action
from dfe_engine.auth.rbac_scopes import scopes_dict

router = APIRouter(prefix="/orgs", tags=["Organisations"])


# -- Request / Response models -----------------------------------------------


class CreateOrgRequest(BaseModel):
    name: str = Field(description="Unique org name (used as identifier)")
    display_name: str = Field(default="", description="Human-readable label")
    org_ids: list[str] = Field(
        default_factory=list,
        description="Tenant IDs for ClickHouse row-level security",
    )
    dedicated_database: bool = Field(
        default=False,
        description="Whether to provision a dedicated ClickHouse database",
    )


class UpdateOrgRequest(BaseModel):
    display_name: str | None = Field(None, description="Human-readable label")
    org_ids: list[str] | None = Field(None, description="Tenant IDs")
    enabled: bool | None = Field(None, description="Enable or disable the org")
    dedicated_database: bool | None = Field(
        None,
        description="Enable or disable a dedicated ClickHouse database",
    )
    confirm_merge: bool = Field(
        default=False,
        description="Required when disabling dedicated_database — confirms data migration is handled",
    )


class OrgResponse(BaseModel):
    name: str
    display_name: str
    org_ids: list[str]
    enabled: bool
    dedicated_database: bool
    created_at: str
    updated_at: str


# -- Endpoints ---------------------------------------------------------------


@router.post(
    "",
    response_model=OrgResponse,
    status_code=201,
    dependencies=[Depends(require_action(scopes_dict["org_write"]))],
)
async def create_org(
    body: CreateOrgRequest,
    user: CurrentUser,
    request: Request,
):
    """Create a new customer organisation (admin only).

    If HyperDX integration is enabled, the lifecycle manager handles
    team creation as part of org provisioning.  Failure is non-fatal.
    """
    from dfe_engine.orgs.registry import OrgRegistry

    registry: OrgRegistry = request.app.state.org_registry
    if registry.get(body.name) is not None:
        raise HTTPException(
            status_code=409,
            detail={"code": "conflict", "message": f"Org '{body.name}' already exists"},
        )

    from dfe_engine.orgs.lifecycle import OrgLifecycleManager

    lifecycle: OrgLifecycleManager = request.app.state.org_lifecycle
    org = await lifecycle.create_org(
        body.name,
        org_ids=body.org_ids,
        display_name=body.display_name,
        dedicated_database=body.dedicated_database,
        admin_id=user.user_id,
    )

    return _org_response(org)


@router.get(
    "",
    response_model=list[OrgResponse],
    dependencies=[Depends(require_action(scopes_dict["org_read"]))],
)
async def list_orgs(
    user: CurrentUser,
    request: Request,
):
    """List all organisations."""
    from dfe_engine.orgs.registry import OrgRegistry

    registry: OrgRegistry = request.app.state.org_registry
    return [_org_response(o) for o in registry.list()]


@router.get(
    "/{name}",
    response_model=OrgResponse,
    dependencies=[Depends(require_action(scopes_dict["org_read"]))],
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
    return _org_response(org)


@router.put(
    "/{name}",
    response_model=OrgResponse,
    dependencies=[Depends(require_action(scopes_dict["org_write"]))],
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
    existing = registry.get(name)
    if existing is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Org '{name}' not found"},
        )

    # Handle dedicated_database toggle via lifecycle manager
    if (
        body.dedicated_database is not None
        and body.dedicated_database != existing.dedicated_database
    ):
        from dfe_engine.orgs.lifecycle import OrgLifecycleManager

        lifecycle: OrgLifecycleManager = request.app.state.org_lifecycle
        try:
            await lifecycle.toggle_dedicated_db(
                name,
                enabled=body.dedicated_database,
                confirm_merge=body.confirm_merge,
                admin_id=user.user_id,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail={"code": "confirmation_required", "message": str(exc)},
            ) from exc

    # Apply remaining field updates
    update_fields: dict[str, object] = {}
    if body.display_name is not None:
        update_fields["display_name"] = body.display_name
    if body.org_ids is not None:
        update_fields["org_ids"] = body.org_ids
    if body.enabled is not None:
        update_fields["enabled"] = body.enabled

    if update_fields:
        org = registry.update(name, **update_fields)
    else:
        org = registry.get(name)
        if org is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "not_found", "message": f"Org '{name}' not found"},
            )

    return _org_response(org)


@router.delete(
    "/{name}",
    status_code=204,
    dependencies=[Depends(require_action(scopes_dict["org_delete"]))],
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

    from dfe_engine.orgs.lifecycle import OrgLifecycleManager

    lifecycle: OrgLifecycleManager = request.app.state.org_lifecycle
    await lifecycle.delete_org(name, admin_id=user.user_id)


# -- Helpers -----------------------------------------------------------------


def _org_response(org: object) -> OrgResponse:
    """Build an OrgResponse from an Org model."""
    return OrgResponse(
        name=org.name,  # type: ignore[attr-defined]
        display_name=org.display_name,  # type: ignore[attr-defined]
        org_ids=org.org_ids,  # type: ignore[attr-defined]
        enabled=org.enabled,  # type: ignore[attr-defined]
        dedicated_database=org.dedicated_database,  # type: ignore[attr-defined]
        created_at=org.created_at,  # type: ignore[attr-defined]
        updated_at=org.updated_at,  # type: ignore[attr-defined]
    )


# -- Background helpers (kept for reference; HyperDX now handled by lifecycle) --


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
