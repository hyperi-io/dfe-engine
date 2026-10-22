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
deploy-repo change, before the org itself is written. A create or update is refused
a tenant id a hunt still reads for another org, on main or on a review branch not yet
merged, because in production+team the org is written while its hunts' rewrite waits
on review.

The ``default`` org is reserved: every record that names no org lands in it, so it
cannot be deleted, disabled or lose its tenant id. An org another org's ingest
credential binds, or a fetched source names, cannot be deleted either. The apps'
tenancy is compiled from the orgs, so an update or delete is followed by the same
reconcile a source write runs.
"""

import functools
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from scalo.concurrency import run_blocking
from scalo.logger import logger

from dfe_engine.api.deps import (
    ClickHouseClient,
    CurrentUser,
    OptionalHuntConfigReg,
    Settings,
    check_action,
    get_source_registry_optional,
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
from dfe_engine.orgs.models import ORG_NAME_PATTERN, Org, ReservedOrgError, TenantId
from dfe_engine.orgs.registry import (
    AnyOrgRegistry,
    OrgBoundError,
    OrgCollisionError,
    OrgExistsError,
    refuse_reserved_update,
)
from dfe_engine.orgs.tenant_scope import colliding_org, org_tenant_ids

if TYPE_CHECKING:
    from dfe_engine.api.v1.sources import AppsSync

# Writes take turns with the hunt routes, whose hunts an org write rewrites.
router = APIRouter(prefix="/orgs", tags=["Organisations"], dependencies=[WRITE_TURN])


# -- Request / Response models -----------------------------------------------


class CreateOrgRequest(BaseModel):
    """Body for creating an org."""

    name: str = Field(description="Unique org name (used as identifier)", pattern=ORG_NAME_PATTERN)
    display_name: str = Field(default="", description="Human-readable label")
    org_ids: list[TenantId] = Field(
        default_factory=list,
        description="Tenant IDs for ClickHouse row-level security",
    )


class UpdateOrgRequest(BaseModel):
    """Body for updating an org, where an omitted field keeps its value."""

    display_name: str | None = Field(None, description="Human-readable label")
    org_ids: list[TenantId] | None = Field(None, description="Tenant IDs")
    enabled: bool | None = Field(None, description="Enable or disable the org")


class OrgResponse(BaseModel):
    """An org as the API returns it."""

    name: str
    display_name: str
    org_ids: list[str]
    enabled: bool
    created_at: str
    updated_at: str


class AvailableOrgIdsResponse(BaseModel):
    """The ``_org_id`` values found in the data database."""

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
    registry: AnyOrgRegistry = request.app.state.org_registry
    lifecycle: OrgLifecycleManager = request.app.state.org_lifecycle
    try:
        # Held until the record is in, so a create that would lose is refused before its hunts move.
        with registry.reserved(body.name, body.org_ids):
            outcome = await run_blocking(
                functools.partial(_create_hunts_first, hunts, body, user.user_id)
            )
            try:
                org = await lifecycle.create_org(
                    body.name,
                    org_ids=body.org_ids,
                    display_name=body.display_name,
                    admin_id=user.user_id,
                )
            except OrgExistsError, OrgCollisionError:
                # A writer that holds nothing, such as the startup seed, got there first.
                await run_blocking(
                    functools.partial(_put_hunts_back, registry, hunts, body.name, user.user_id)
                )
                raise
    except (OrgExistsError, OrgCollisionError) as exc:
        raise _conflict(exc) from exc
    apply_review_headers(response, outcome)
    return _org_response(org)


def _create_hunts_first(hunts: Any, body: CreateOrgRequest, actor: str) -> WriteOutcome | None:
    """Store the new org's tenant ids in the hunts naming it.

    Raises:
        HTTPException: 409 ``tenant_id_held`` for a tenant id a hunt still reads for
            another org.
    """
    tenant_ids = org_tenant_ids(body)
    refuse_held_tenant_ids(hunts, body.name, tenant_ids)
    return store_hunt_tenant_ids(hunts, body.name, tenant_ids, actor)


def _put_hunts_back(registry: AnyOrgRegistry, hunts: Any, name: str, actor: str) -> None:
    """Store in the hunts naming *name* the tenant ids of whichever org holds it now. Never raises.

    A create refused after its hunt write leaves them as the org that won the name, or
    no org at all, would have them.
    """
    now = registry.get(name)
    try:
        store_hunt_tenant_ids(hunts, name, None if now is None else org_tenant_ids(now), actor)
    except Exception as exc:  # the create's 409 stands whatever the hunt write does
        logger.error("hunts not put back after a refused org create", org=name, error=str(exc))


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
    registry: AnyOrgRegistry = request.app.state.org_registry
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
    registry: AnyOrgRegistry = request.app.state.org_registry
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
    registry: AnyOrgRegistry = request.app.state.org_registry
    org, outcome = await run_blocking(
        functools.partial(_update_org, registry, hunts, name, body, user.user_id)
    )
    if body.org_ids is not None or body.enabled is not None:
        await run_blocking(functools.partial(reconcile_tenancy, request, user))
    apply_review_headers(response, outcome)
    return _org_response(org)


def _update_org(
    registry: AnyOrgRegistry, hunts: Any, name: str, body: UpdateOrgRequest, actor: str
) -> tuple[Org, WriteOutcome | None]:
    """Store the org's tenant ids in its hunts, then write the org.

    Raises:
        HTTPException: 404 ``not_found``, 409 ``reserved_org`` for an update that would
            disable the default org or take its tenant id, 409 ``conflict`` for a
            tenant id another org holds, or 409 ``tenant_id_held`` for one a hunt
            still reads for another org.
    """
    existing = registry.get(name)
    if existing is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Org '{name}' not found"},
        )
    try:
        refuse_reserved_update(existing.model_copy(update=body.model_dump(exclude_none=True)))
    except ReservedOrgError as exc:
        raise HTTPException(
            status_code=409, detail={"code": "reserved_org", "message": str(exc)}
        ) from exc
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
    tenant_ids = org_tenant_ids(after)
    if body.org_ids is not None:
        refuse_held_tenant_ids(hunts, name, tenant_ids)
    outcome = store_hunt_tenant_ids(hunts, name, tenant_ids, actor)
    if not update_fields:
        return existing, outcome
    # Another replica's write since the checks above is judged again by the registry.
    try:
        org = registry.update(name, **update_fields)
    except KeyError as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Org '{name}' not found"},
        ) from exc
    except ReservedOrgError as exc:
        raise HTTPException(
            status_code=409, detail={"code": "reserved_org", "message": str(exc)}
        ) from exc
    except OrgCollisionError as exc:
        raise _conflict(exc) from exc
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
    """Delete an organisation (admin only), and the ingest credentials it owns.

    Every hunt naming the org loses its stored tenant ids first and keeps the name,
    so it fails as ``unknown_customers`` and holds its watermark until it is edited,
    rather than reading the rows the org left behind. A production+team posture
    routes that hunt change to review, and the ``X-DFE-Review-Required`` header says so.

    The reserved ``default`` org cannot be deleted, nor can an org another org's
    ingest credential binds or a fetched source names: each answers 409.
    """
    registry: AnyOrgRegistry = request.app.state.org_registry
    lifecycle: OrgLifecycleManager = request.app.state.org_lifecycle
    try:
        # Held until the record is gone, so a delete that would be refused is refused before
        # its hunts move, and no credential write binds the org meanwhile.
        with registry.deleting(name):
            outcome = await run_blocking(
                functools.partial(_delete_hunts_first, hunts, name, user.user_id)
            )
            await lifecycle.delete_org(name, admin_id=user.user_id)
    except KeyError as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Org '{name}' not found"},
        ) from exc
    except ReservedOrgError as exc:
        raise HTTPException(
            status_code=409, detail={"code": "reserved_org", "message": str(exc)}
        ) from exc
    except OrgBoundError as exc:
        raise HTTPException(
            status_code=409, detail={"code": "org_bound", "message": str(exc)}
        ) from exc
    await run_blocking(functools.partial(reconcile_tenancy, request, user))
    apply_review_headers(response, outcome)


def _delete_hunts_first(hunts: Any, name: str, actor: str) -> WriteOutcome | None:
    """Refuse a delete a fetched source forbids, then take the org's tenant ids out of its hunts.

    Raises:
        HTTPException: 409 ``org_in_use`` for an org a fetched source names.
    """
    refuse_org_named_by_sources(name)
    return store_hunt_tenant_ids(hunts, name, None, actor)


def refuse_org_named_by_sources(name: str) -> None:
    """Refuse to remove an org a fetched source names, in any of its versions.

    Its fetcher would otherwise be compiled for an org that is gone and stop.

    Raises:
        HTTPException: 409 ``org_in_use`` naming each source.
    """
    registry = get_source_registry_optional()
    if registry is None:
        return
    named = sorted(
        source.source
        for source in registry.get_all_sources()
        if any(v.fetcher is not None and v.fetcher.org == name for v in source.versions.values())
    )
    if named:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "org_in_use",
                "message": f"Org '{name}' is the org of fetched source(s) "
                f"{', '.join(named)}: move them to another org first",
            },
        )


def reconcile_tenancy(request: Request, user: Any) -> AppsSync | None:
    """Recompile the tenancy the orgs and ingest credentials feed the apps. Never raises.

    A failure is logged, and the next org, credential or source write retries it.

    Returns:
        The overlay writes made and why they stopped, if they did. None for a
        deployment with no source registry, which has nothing to recompile.
    """
    from dfe_engine.api.v1.sources import reconcile_derived_apps

    registry = get_source_registry_optional()
    if registry is None:
        return None
    synced = reconcile_derived_apps(request, user, registry)
    if synced.error:
        logger.error("apps not reconciled with the orgs", error=synced.error)
    return synced


# -- Helpers -----------------------------------------------------------------


def refuse_held_tenant_ids(hunts: Any, org: str, tenant_ids: list[str]) -> None:
    """Refuse ``org`` a tenant id a hunt still reads for another org, on main or in review.

    A hunt holding a deleted or changed org's ids until its rewrite is merged would
    otherwise read the next org's rows under the old org's name.

    Args:
        hunts: The hunt registry, or None where the deployment carries none.
        org: The org being created or updated.
        tenant_ids: Its tenant ids once written.

    Raises:
        HTTPException: 409 ``tenant_id_held`` naming each hunt.
    """
    if hunts is None:
        return
    held = hunts.tenant_id_holders(org, tenant_ids)
    if not held:
        return
    where = ", ".join(
        f"hunt '{h.hunt}' reads {', '.join(h.tenant_ids)} for org '{h.org}' "
        + (f"once review branch '{h.branch}' merges" if h.branch else "on main")
        for h in held
    )
    raise HTTPException(
        status_code=409,
        detail={
            "code": "tenant_id_held",
            "message": (
                f"Org '{org}' cannot take a tenant id a hunt still reads for another org: "
                f"{where}. Merge or close that review, or edit the hunt, first"
            ),
            "held": [
                {"hunt": h.hunt, "org": h.org, "tenant_ids": h.tenant_ids, "branch": h.branch}
                for h in held
            ],
        },
    )


def store_hunt_tenant_ids(
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


def _conflict(exc: Exception) -> HTTPException:
    """409 ``conflict`` carrying the registry's reason."""
    return HTTPException(status_code=409, detail={"code": "conflict", "message": str(exc)})


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
