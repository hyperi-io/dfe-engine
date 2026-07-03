#  Project:      dfe-engine
#  File:         api/v1/lifecycle.py
#  Purpose:      Governed Ops - service lifecycle (start/stop/pause) via gitops
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Start / stop / pause any DFE service through the gitops state dial.

GET  /api/v1/lifecycle             -> list services + tier + current state
POST /api/v1/lifecycle/{name}      -> set state (per-service RBAC; pinned -> 404)

Per-service RBAC: dfe-* apps need ``lifecycle:app:<name>`` (operator-grantable);
backing services need ``lifecycle:backing:<name>`` (admin/infra roles only);
pinned services (ClickHouse, OpenBao, the cluster, DNS, Argo) have no lifecycle
API. Every change is a git commit (commit != deployment - Argo reconciles).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from dfe_engine.api.deps import CurrentUser, require_action
from dfe_engine.auth.audit import audit_resource_change
from dfe_engine.auth.engine import authorize
from dfe_engine.gitcrud import GitCrud, ResourceNotFoundError
from dfe_engine.gitcrud.auto_merge import apply_auto_merge, resolve_state
from dfe_engine.governance.lifecycle import (
    LifecycleError,
    LifecycleState,
    ServiceLifecycle,
    ServiceTier,
    required_action,
    resolve,
    services,
    set_state,
)

router = APIRouter(prefix="/lifecycle", tags=["Governed Ops: Lifecycle"])


class ServiceInfo(BaseModel):
    name: str
    tier: str
    state: str


class LifecycleRequest(BaseModel):
    state: LifecycleState


class LifecycleResponse(BaseModel):
    name: str
    state: str
    changed: bool
    commit_sha: str | None = None
    # commit != deployment: an approval/merge + Argo reconcile sit in between.
    pending_reconcile: bool = True


def _gitcrud(request: Request) -> GitCrud:
    gc = getattr(request.app.state, "gitcrud", None)
    if gc is None:
        raise HTTPException(
            status_code=503,
            detail={"code": "not_configured", "message": "gitops is not enabled"},
        )
    return gc


def _current_state(gc: GitCrud, svc: ServiceLifecycle) -> str:
    try:
        return str(gc.get(svc.cls, svc.resource).get(svc.path, "running"))
    except ResourceNotFoundError:
        return "running"  # no dial yet = default running


@router.get("", dependencies=[Depends(require_action("lifecycle:read"))])
async def list_services(user: CurrentUser, request: Request) -> list[ServiceInfo]:
    """List services with their tier and current lifecycle state."""
    gc = _gitcrud(request)
    out: list[ServiceInfo] = []
    for svc in services():
        state = "pinned" if svc.tier == ServiceTier.PINNED else _current_state(gc, svc)
        out.append(ServiceInfo(name=svc.name, tier=svc.tier.value, state=state))
    return out


@router.post("/{name}", response_model=LifecycleResponse)
async def set_lifecycle(
    name: str, body: LifecycleRequest, user: CurrentUser, request: Request
) -> LifecycleResponse:
    """Set a service's lifecycle state - per-service RBAC; pinned services are 404."""
    try:
        svc = resolve(name)
    except KeyError as exc:
        raise HTTPException(
            404, detail={"code": "not_found", "message": f"unknown service '{name}'"}
        ) from exc
    if svc.tier == ServiceTier.PINNED:
        raise HTTPException(
            404, detail={"code": "not_found", "message": f"'{name}' has no lifecycle API (pinned)"}
        )

    action = required_action(name, body.state)
    decision = authorize(user, action, role_config=request.app.state.role_config)
    if not decision.allowed:
        raise HTTPException(403, detail={"code": "forbidden", "message": decision.reason})

    gc = _gitcrud(request)
    try:
        res = set_state(gc, name, body.state, user.user_id)
    except LifecycleError as exc:
        raise HTTPException(403, detail={"code": "forbidden", "message": str(exc)}) from exc
    audit_resource_change(
        user.user_id, "lifecycle", name, body.state.value, {"commit": res.commit_sha}
    )
    if res.changed:
        settings = request.app.state.settings
        state = resolve_state(gc, environment=settings.env, mode=settings.gitops.mode)
        apply_auto_merge(
            state,
            environment=settings.env,
            rbac_class=svc.cls,
            actor=user.user_id,
            resource=f"lifecycle/{name}",
            commit_sha=res.commit_sha,
        )
    return LifecycleResponse(
        name=name, state=body.state.value, changed=res.changed, commit_sha=res.commit_sha
    )
