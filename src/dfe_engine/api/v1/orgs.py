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

import os
from typing import TYPE_CHECKING

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from scalo.logger import logger

from dfe_engine.api.deps import CurrentUser, check_action, is_action_allowed, require_action
from dfe_engine.auth import Scope
from dfe_engine.auth.rbac_scopes import scopes_dict

if TYPE_CHECKING:
    from dfe_engine.orgs.models import Org
    from dfe_engine.settings import DFESettings

# DFE_ORG_PROVISIONING_ENABLED truthy values (mirrors app.py startup reconcile gate).
_PROVISIONING_TRUE = ("true", "1", "yes")

router = APIRouter(prefix="/orgs", tags=["Organisations"])


# -- Request / Response models -----------------------------------------------


class CreateOrgRequest(BaseModel):
    name: str = Field(description="Unique org name (used as identifier)")
    display_name: str = Field(default="", description="Human-readable label")
    org_ids: list[str] = Field(
        default_factory=list,
        description="Tenant IDs for ClickHouse row-level security",
    )
    domains: list[str] = Field(
        default_factory=list,
        description="Email domains this org claims (lowercased); map external OIDC logins to this org",
    )


class UpdateOrgRequest(BaseModel):
    display_name: str | None = Field(None, description="Human-readable label")
    org_ids: list[str] | None = Field(None, description="Tenant IDs")
    domains: list[str] | None = Field(
        None, description="Email domains this org claims (lowercased)"
    )
    enabled: bool | None = Field(None, description="Enable or disable the org")


class OrgResponse(BaseModel):
    name: str
    display_name: str
    org_ids: list[str]
    domains: list[str]
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
    background_tasks: BackgroundTasks,
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
    try:
        org = await lifecycle.create_org(
            body.name,
            org_ids=body.org_ids,
            display_name=body.display_name,
            admin_id=user.user_id,
        )
    except ValueError as exc:
        # check-then-act race: a concurrent create landed the org between the
        # get() above and here. registry.create raises ValueError -> 409, not 500.
        raise HTTPException(
            status_code=409,
            detail={"code": "conflict", "message": f"Org '{body.name}' already exists"},
        ) from exc
    # Domains are not part of the lifecycle create signature (phase-3 owned), so
    # persist them via the registry once the org exists.
    if body.domains:
        org = registry.update(body.name, domains=body.domains)

    _schedule_ch_reconcile(request, background_tasks)
    return _org_response(org)


@router.get(
    "",
    response_model=list[OrgResponse],
)
async def list_orgs(
    user: CurrentUser,
    request: Request,
):
    """List organisations visible to the caller.

    System-scope org:read holders see every org; org-scope holders see
    only the orgs their grants cover.
    """
    from dfe_engine.orgs.registry import OrgRegistry

    registry: OrgRegistry = request.app.state.org_registry
    if is_action_allowed(request, user, scopes_dict["org_read"]):
        return [_org_response(o) for o in registry.list()]
    return [
        _org_response(o)
        for o in registry.list()
        if is_action_allowed(
            request, user, scopes_dict["org_read"], scope=Scope(type="org", id=o.name)
        )
    ]


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
    background_tasks: BackgroundTasks,
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
    if body.domains is not None:
        update_fields["domains"] = body.domains
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

    _schedule_ch_reconcile(request, background_tasks)
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
    background_tasks: BackgroundTasks,
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

    _schedule_ch_reconcile(request, background_tasks)


# -- Helpers -----------------------------------------------------------------


def _org_response(org: Org) -> OrgResponse:
    """Build an OrgResponse from an Org model."""
    return OrgResponse(
        name=org.name,
        display_name=org.display_name,
        org_ids=org.org_ids,
        domains=org.domains,
        enabled=org.enabled,
        created_at=org.created_at,
        updated_at=org.updated_at,
    )


def _schedule_ch_reconcile(request: Request, background_tasks: BackgroundTasks) -> None:
    """Schedule a background CH-RBAC reconcile after an org mutation.

    No-op unless ClickHouse is configured AND DFE_ORG_PROVISIONING_ENABLED is set.
    After the Phase-2 isolation pivot adding/removing an org needs NO per-org CH
    DDL (the tenant axis is the fixed users + ONE row policy per ``_org_id`` table,
    org-agnostic), so this reconcile is just an idempotent "ensure those fixed
    objects exist + refresh the ``dfe_meta.orgs`` projection". It runs OFF the
    request path (FastAPI background task) and is fully non-fatal: a CH error can
    never block or fail the CRUD (see ``_run_ch_reconcile``).
    """
    settings: DFESettings = request.app.state.settings
    if not settings.clickhouse.host:
        return
    if os.environ.get("DFE_ORG_PROVISIONING_ENABLED", "").lower() not in _PROVISIONING_TRUE:
        return
    registry = getattr(request.app.state, "org_registry", None)
    orgs = registry.list() if registry is not None else []
    background_tasks.add_task(_run_ch_reconcile, settings, orgs)


def _run_ch_reconcile(settings: DFESettings, orgs: list) -> None:
    """Best-effort CH-RBAC reconcile body (runs in the background threadpool).

    Builds the admin client + secrets store the same way the startup and
    governance-endpoint reconciles do, then calls the ONE reconcile entry point.
    Any failure (CH unreachable, secrets seam down, bad statement) is swallowed
    and logged - the org CRUD that scheduled this has already returned.
    """
    try:
        from dfe_engine.clickhouse.clickhouse_manager import ClickHouseManager
        from dfe_engine.governance.ch import reconcile_ch_rbac
        from dfe_engine.secrets import build_secrets
        from dfe_engine.settings import get_clickhouse_config

        admin_client = (
            ClickHouseManager.get_instance(get_clickhouse_config(settings))
            .get_clickhouse_client()
            ._client
        )
        reconcile_ch_rbac(
            admin_client,
            secrets_store=build_secrets(settings.secrets),
            orgs=orgs,
        )
    except Exception:
        logger.exception("Background CH RBAC reconcile after org mutation failed")
