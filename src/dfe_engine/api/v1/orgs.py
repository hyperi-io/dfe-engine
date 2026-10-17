#  Project:      dfe-engine
#  File:         api/v1/orgs.py
#  Purpose:      Org CRUD REST endpoints (admin for write, read for list/get)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Organisation management router -- CRUD for customer organisations.

POST   /api/v1/orgs                -> Create org
GET    /api/v1/orgs                -> List orgs
GET    /api/v1/orgs/available-ids  -> Tenant ids the data carries
GET    /api/v1/orgs/{name}         -> Get org
PUT    /api/v1/orgs/{name}         -> Update org
DELETE /api/v1/orgs/{name}         -> Delete org

Write endpoints require admin role (org:write).
Read endpoints require org:read.

A hunt stores the tenant ids each customer org resolved to, so every create, update
and delete stores the org's tenant ids afresh in each hunt naming it, as one
deploy-repo change, before the org itself is written.
"""

import functools
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from scalo.concurrency import run_blocking

from dfe_engine.api.deps import (
    ClickHouseClient,
    CurrentUser,
    OptionalHuntConfigReg,
    Settings,
    check_action,
    is_action_allowed,
    require_action,
)
from dfe_engine.api.errors import backend_failure
from dfe_engine.api.pagination import (
    PaginatedResponse,
    PaginationParams,
    apply_search,
    apply_sort,
)
from dfe_engine.api.review import apply_review_headers, review_audit_detail
from dfe_engine.api.write_turn import WRITE_TURN
from dfe_engine.auth import Scope
from dfe_engine.auth.audit import audit_resource_change
from dfe_engine.auth.rbac_scopes import scopes_dict
from dfe_engine.gitcrud.routing import WriteOutcome
from dfe_engine.orgs.available_ids import DEFAULT_LIMIT, OrgIdDiscoveryError
from dfe_engine.orgs.available_ids import available_org_ids as discover_available_org_ids
from dfe_engine.orgs.lifecycle import OrgLifecycleManager
from dfe_engine.orgs.models import ORG_NAME_PATTERN, Org
from dfe_engine.orgs.registry import OrgRegistry
from dfe_engine.orgs.tenant_scope import colliding_org, org_tenant_ids

# Writes take turns with the hunt routes, whose hunts an org write rewrites.
router = APIRouter(prefix="/orgs", tags=["Organisations"], dependencies=[WRITE_TURN])


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


class AvailableOrgIdsResponse(BaseModel):
    database: str = Field(description="Data database the ids were read from")
    org_ids: list[str] = Field(
        default_factory=list,
        description="Distinct _org_id values present across that database's tables",
    )


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
    response: Response,
    hunts: OptionalHuntConfigReg,
):
    """Create a new customer organisation (admin only).

    If HyperDX integration is enabled, the lifecycle manager handles
    team creation as part of org provisioning.  Failure is non-fatal.

    A hunt already naming the org, such as one whose org was deleted, has the new
    org's tenant ids stored first. A production+team posture routes that hunt
    change to review, and the ``X-DFE-Review-Required`` header says so.
    """
    registry: OrgRegistry = request.app.state.org_registry
    outcome = await run_blocking(
        functools.partial(_create_hunts_first, registry, hunts, body, user.user_id)
    )

    lifecycle: OrgLifecycleManager = request.app.state.org_lifecycle
    org = await lifecycle.create_org(
        body.name,
        org_ids=body.org_ids,
        display_name=body.display_name,
        admin_id=user.user_id,
    )
    apply_review_headers(response, outcome)
    return _org_response(org)


def _create_hunts_first(
    registry: OrgRegistry, hunts: Any, body: CreateOrgRequest, actor: str
) -> WriteOutcome | None:
    """Refuse a taken name or a collision, then store the new org's tenant ids in its hunts.

    Raises:
        HTTPException: 409 ``conflict`` for a name taken or a tenant id another org holds.
    """
    if registry.get(body.name) is not None:
        raise HTTPException(
            status_code=409,
            detail={"code": "conflict", "message": f"Org '{body.name}' already exists"},
        )
    other = colliding_org(body.name, body.org_ids, registry.list())
    if other is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "conflict",
                "message": f"Org '{body.name}' collides with tenant-fenced org '{other.name}'",
            },
        )
    return _store_hunt_tenant_ids(hunts, body.name, org_tenant_ids(body), actor)


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
    "/available-ids",
    response_model=AvailableOrgIdsResponse,
    dependencies=[Depends(require_action(scopes_dict["org_write"]))],
)
async def available_org_ids(
    user: CurrentUser,
    ch: ClickHouseClient,
    settings: Settings,
    limit: int = Query(
        default=DEFAULT_LIMIT,
        ge=1,
        le=1000,
        description="Most tenant ids to return -- a suggestion list, not an export",
    ),
) -> AvailableOrgIdsResponse:
    """Tenant ids the deployment's data actually carries (admin only).

    Suggestions for the org form's Organisation IDs field, so an operator picks
    an id that matches rows rather than typing one that matches none. Declared
    above ``/{name}`` so the literal path wins the match.
    """
    database = settings.clickhouse.effective_data_database
    try:
        org_ids = discover_available_org_ids(ch, database=database, limit=limit)
    except OrgIdDiscoveryError as exc:
        raise backend_failure(
            503,
            "discovery_failed",
            f"the tenant ids in {database} could not be read; the engine log has "
            "ClickHouse's reason",
            exc,
            event="tenant id discovery failed",
        ) from exc
    return AvailableOrgIdsResponse(database=database, org_ids=org_ids)


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
    response: Response,
    hunts: OptionalHuntConfigReg,
):
    """Update an organisation (admin only).

    The org's tenant ids are stored afresh in every hunt naming it before the org is
    written, which also repairs a hunt holding stale ones. A production+team posture
    routes that hunt change to review, and the ``X-DFE-Review-Required`` header says
    so. An org's name is its identity, so it cannot be changed here.
    """
    registry: OrgRegistry = request.app.state.org_registry
    org, outcome = await run_blocking(
        functools.partial(_update_org, registry, hunts, name, body, user.user_id)
    )
    apply_review_headers(response, outcome)
    return _org_response(org)


def _update_org(
    registry: OrgRegistry, hunts: Any, name: str, body: UpdateOrgRequest, actor: str
) -> tuple[Org, WriteOutcome | None]:
    """Store the org's tenant ids in its hunts, then write the org.

    Raises:
        HTTPException: 404 ``not_found``, or 409 ``conflict`` for a tenant id another
            org holds.
    """
    existing = registry.get(name)
    if existing is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Org '{name}' not found"},
        )
    if body.org_ids is not None:
        other = colliding_org(name, body.org_ids, registry.list())
        if other is not None:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "conflict",
                    "message": f"Org '{name}' collides with tenant-fenced org '{other.name}'",
                },
            )

    # Apply field updates
    update_fields: dict[str, object] = {}
    if body.display_name is not None:
        update_fields["display_name"] = body.display_name
    if body.org_ids is not None:
        update_fields["org_ids"] = body.org_ids
    if body.enabled is not None:
        update_fields["enabled"] = body.enabled

    after = existing.model_copy(update=update_fields)
    outcome = _store_hunt_tenant_ids(hunts, name, org_tenant_ids(after), actor)
    org = registry.update(name, **update_fields) if update_fields else existing
    return org, outcome


@router.delete(
    "/{name}",
    status_code=204,
    dependencies=[Depends(require_action(scopes_dict["org_delete"]))],
)
async def delete_org(
    name: str,
    user: CurrentUser,
    request: Request,
    response: Response,
    hunts: OptionalHuntConfigReg,
):
    """Delete an organisation (admin only).

    Every hunt naming the org loses its stored tenant ids first and keeps the name,
    so it fails as ``unknown_customers`` and holds its watermark until it is edited,
    rather than reading the rows the org left behind. A production+team posture
    routes that hunt change to review, and the ``X-DFE-Review-Required`` header says so.
    """
    registry: OrgRegistry = request.app.state.org_registry
    outcome = await run_blocking(
        functools.partial(_delete_hunts_first, registry, hunts, name, user.user_id)
    )

    lifecycle: OrgLifecycleManager = request.app.state.org_lifecycle
    await lifecycle.delete_org(name, admin_id=user.user_id)
    apply_review_headers(response, outcome)


def _delete_hunts_first(
    registry: OrgRegistry, hunts: Any, name: str, actor: str
) -> WriteOutcome | None:
    """Take the org's tenant ids out of its hunts before the org goes.

    Raises:
        HTTPException: 404 ``not_found``.
    """
    if registry.get(name) is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Org '{name}' not found"},
        )
    return _store_hunt_tenant_ids(hunts, name, None, actor)


# -- Helpers -----------------------------------------------------------------


def _store_hunt_tenant_ids(
    hunts: Any, org: str, tenant_ids: list[str] | None, actor: str
) -> WriteOutcome | None:
    """Store ``org``'s tenant ids afresh in every hunt naming it, as one change.

    It runs before the org is written, so a hunt write that fails leaves the org as
    it was and the same request can be sent again.

    Args:
        hunts: The hunt registry, or None where the deployment carries none.
        org: The org being created, updated or deleted.
        tenant_ids: Its tenant ids once written, or None when it is being deleted.
        actor: Who changed the org.

    Returns:
        Where the hunt change landed, or None when no hunt changed or off gitops.
    """
    if hunts is None:
        return None
    names, outcome = hunts.store_org_tenant_ids(org, tenant_ids, created_by=actor)
    detail = {"org": org, **(review_audit_detail(outcome) or {})}
    for hunt in names:
        audit_resource_change(actor, "hunt", hunt, "updated", detail)
    return outcome


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
