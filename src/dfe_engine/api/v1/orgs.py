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

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from dfe_engine.api.deps import CurrentUser, check_action, is_action_allowed, require_action
from dfe_engine.api.pagination import (
    PaginatedResponse,
    PaginationParams,
    apply_search,
    apply_sort,
)
from dfe_engine.auth import Scope
from dfe_engine.auth.rbac_scopes import scopes_dict
from dfe_engine.orgs.models import ORG_NAME_PATTERN

if TYPE_CHECKING:
    from dfe_engine.orgs.models import Org

router = APIRouter(prefix="/orgs", tags=["Organisations"])


# -- Request / Response models -----------------------------------------------


class CreateOrgRequest(BaseModel):
    name: str = Field(description="Unique org name (used as identifier)", pattern=ORG_NAME_PATTERN)
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
        admin_id=user.user_id,
    )

    return _org_response(org)


@router.get(
    "",
    response_model=PaginatedResponse[OrgResponse],
)
async def list_orgs(
    user: CurrentUser,
    request: Request,
    pagination: PaginationParams = Depends(),
    search: str | None = Query(None, description="Search in org name/display name"),
    sort_by: str | None = Query(None, description="Sort field (name, display_name, updated_at)"),
    sort_order: str = Query("asc", description="Sort order: asc/desc"),
):
    """List organisations visible to the caller, paginated.

    System-scope org:read holders see every org; org-scope holders see
    only the orgs their grants cover.
    """
    from dfe_engine.orgs.registry import OrgRegistry

    registry: OrgRegistry = request.app.state.org_registry
    if is_action_allowed(request, user, scopes_dict["org_read"]):
        visible = list(registry.list())
    else:
        visible = [
            o
            for o in registry.list()
            if is_action_allowed(
                request, user, scopes_dict["org_read"], scope=Scope(type="org", id=o.name)
            )
        ]
    rows = [_org_response(o).model_dump() for o in visible]
    rows = apply_search(rows, search, ["name", "display_name"])
    rows = apply_sort(rows, sort_by, sort_order)
    summaries = [OrgResponse.model_validate(row) for row in rows]
    return PaginatedResponse.from_list(summaries, pagination.page, pagination.per_page)


@router.get(
    "/{name}",
    response_model=OrgResponse,
)
async def get_org(
    name: str,
    user: CurrentUser,
    request: Request,
):
    """Get a single organisation by name (org:read at that org's scope)."""
    from dfe_engine.orgs.registry import OrgRegistry

    check_action(request, user, scopes_dict["org_read"], scope=Scope(type="org", id=name))
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

    # Apply field updates
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


def _org_response(org: Org) -> OrgResponse:
    """Build an OrgResponse from an Org model."""
    return OrgResponse(
        name=org.name,
        display_name=org.display_name,
        org_ids=org.org_ids,
        enabled=org.enabled,
        created_at=org.created_at,
        updated_at=org.updated_at,
    )
