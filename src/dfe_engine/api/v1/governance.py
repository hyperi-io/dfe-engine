#  Project:      dfe-engine
#  File:         api/v1/governance.py
#  Purpose:      Governed Ops Tier-2 - defined actions + admin CRUD
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tier-2 defined actions (the curated big dials) + admin CRUD of actions/policies.

GET  /api/v1/governance/actions                 -> list actions (governance:read)
GET  /api/v1/governance/actions/{name}          -> get an action (governance:read)
POST /api/v1/governance/actions/{name}/invoke   -> invoke (per-action required_action)
POST/PUT/DELETE /api/v1/governance/admin/actions[/{name}]   -> CRUD defs (governance:write)
POST/DELETE     /api/v1/governance/admin/policies[/{name}]  -> CRUD policies (governance:write)

Operators get curated actions via `action:invoke:<name>`; raw class CRUD stays with
admins (governance:write). Everything commits to gitops via the engine.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from dfe_engine.api.deps import CurrentUser, require_action
from dfe_engine.auth.audit import audit_resource_change
from dfe_engine.auth.engine import authorize
from dfe_engine.gitcrud import GitCrud, ResourceNotFoundError
from dfe_engine.gitcrud.auto_merge import apply_auto_merge, resolve_state
from dfe_engine.gitcrud.commit_policy import CommitPolicyError
from dfe_engine.governance import (
    ActionDef,
    ActionForbiddenError,
    ActionStore,
    ProtectedPolicy,
    ProtectedVarError,
)

router = APIRouter(prefix="/governance", tags=["Governed Ops: Actions"])

_POLICY_CLASS = "policies"


class InvokeResponse(BaseModel):
    dry_run: bool
    changed: bool
    commit_sha: str | None = None
    auto_merged: bool = False
    diff: list[dict[str, Any]]


def _gitcrud(request: Request) -> GitCrud:
    gc = getattr(request.app.state, "gitcrud", None)
    if gc is None:
        raise HTTPException(
            status_code=503,
            detail={"code": "not_configured", "message": "gitops is not enabled"},
        )
    return gc


def _actions(request: Request) -> ActionStore:
    return ActionStore(_gitcrud(request))


def _warn_auto_merge(
    request: Request, *, actor: str, resource: str, commit_sha: str | None
) -> None:
    """WARN the conversion for a governance-class write, mirroring helm's badge path.

    No response badge here (some of these endpoints are 201/204) - just the loud
    conversion WARN the spec requires on every mutation that would otherwise have
    been PR-mode.
    """
    settings = request.app.state.settings
    state = resolve_state(_gitcrud(request), environment=settings.env, mode=settings.gitops.mode)
    apply_auto_merge(
        state,
        environment=settings.env,
        rbac_class="governance",
        actor=actor,
        resource=resource,
        commit_sha=commit_sha,
    )


@router.get("/actions", dependencies=[Depends(require_action("governance:read"))])
async def list_actions(user: CurrentUser, request: Request) -> list[str]:
    return _actions(request).list()


@router.get("/actions/{name}", dependencies=[Depends(require_action("governance:read"))])
async def get_action(name: str, user: CurrentUser, request: Request) -> ActionDef:
    try:
        return _actions(request).get(name)
    except ResourceNotFoundError as exc:
        raise HTTPException(404, detail={"code": "not_found", "message": str(exc)}) from exc


@router.post("/actions/{name}/invoke", response_model=InvokeResponse)
async def invoke_action(
    name: str,
    user: CurrentUser,
    request: Request,
    dry_run: bool = Query(default=False),
) -> InvokeResponse:
    """Invoke a defined action - gated on the action's OWN required_action."""
    store = _actions(request)
    try:
        action = store.get(name)
    except ResourceNotFoundError as exc:
        raise HTTPException(404, detail={"code": "not_found", "message": str(exc)}) from exc

    # Per-action RBAC: the action declares the permission needed to run it.
    decision = authorize(user, action.required_action, role_config=request.app.state.role_config)
    if not decision.allowed:
        raise HTTPException(403, detail={"code": "forbidden", "message": decision.reason})

    policy = getattr(request.app.state, "policy_store", None)
    override = authorize(
        user, "helmvars:override", role_config=request.app.state.role_config
    ).allowed
    try:
        res = store.invoke(name, user.user_id, policy=policy, dry_run=dry_run, override=override)
    except ProtectedVarError as exc:
        raise HTTPException(403, detail={"code": "protected_var", "message": str(exc)}) from exc
    except ActionForbiddenError as exc:
        raise HTTPException(403, detail={"code": "action_forbidden", "message": str(exc)}) from exc
    except CommitPolicyError as exc:
        raise HTTPException(403, detail={"code": "policy_violation", "message": str(exc)}) from exc
    if not dry_run:
        audit_resource_change(user.user_id, "action", name, "invoked", {"commit": res.commit_sha})

    auto_merged = False
    if not dry_run and res.changed:
        settings = request.app.state.settings
        state = resolve_state(
            _gitcrud(request), environment=settings.env, mode=settings.gitops.mode
        )
        auto_merged = apply_auto_merge(
            state,
            environment=settings.env,
            rbac_class="governance",
            actor=user.user_id,
            resource=f"action/{name}",
            commit_sha=res.commit_sha,
        )
    return InvokeResponse(
        dry_run=res.dry_run,
        changed=res.changed,
        commit_sha=res.commit_sha,
        auto_merged=auto_merged,
        diff=res.diff,
    )


@router.post(
    "/admin/actions",
    status_code=201,
    dependencies=[Depends(require_action("governance:write"))],
)
async def create_action(body: ActionDef, user: CurrentUser, request: Request) -> ActionDef:
    res = _actions(request).save(body, user.user_id)
    audit_resource_change(user.user_id, "action", body.name, "created")
    if res.changed:
        _warn_auto_merge(
            request,
            actor=user.user_id,
            resource=f"action/{body.name}",
            commit_sha=res.commit_sha,
        )
    return body


@router.delete(
    "/admin/actions/{name}",
    status_code=204,
    dependencies=[Depends(require_action("governance:write"))],
)
async def delete_action(name: str, user: CurrentUser, request: Request) -> None:
    try:
        res = _actions(request).delete(name, user.user_id)
    except ResourceNotFoundError as exc:
        raise HTTPException(404, detail={"code": "not_found", "message": str(exc)}) from exc
    audit_resource_change(user.user_id, "action", name, "deleted")
    if res.changed:
        _warn_auto_merge(
            request, actor=user.user_id, resource=f"action/{name}", commit_sha=res.commit_sha
        )


@router.post(
    "/admin/policies",
    status_code=201,
    dependencies=[Depends(require_action("governance:write"))],
)
async def create_policy(
    body: ProtectedPolicy, user: CurrentUser, request: Request
) -> ProtectedPolicy:
    gc = _gitcrud(request)
    res = gc.put(_POLICY_CLASS, body.name, body.model_dump(), user.user_id)
    audit_resource_change(user.user_id, "policy", body.name, "created")
    if res.changed:
        _warn_auto_merge(
            request,
            actor=user.user_id,
            resource=f"policy/{body.name}",
            commit_sha=res.commit_sha,
        )
    return body


@router.delete(
    "/admin/policies/{name}",
    status_code=204,
    dependencies=[Depends(require_action("governance:write"))],
)
async def delete_policy(name: str, user: CurrentUser, request: Request) -> None:
    gc = _gitcrud(request)
    try:
        res = gc.delete(_POLICY_CLASS, name, user.user_id)
    except ResourceNotFoundError as exc:
        raise HTTPException(404, detail={"code": "not_found", "message": str(exc)}) from exc
    audit_resource_change(user.user_id, "policy", name, "deleted")
    if res.changed:
        _warn_auto_merge(
            request, actor=user.user_id, resource=f"policy/{name}", commit_sha=res.commit_sha
        )


@router.post(
    "/ch-rbac/reconcile",
    dependencies=[Depends(require_action("governance:write"))],
)
async def reconcile_ch_rbac_endpoint(user: CurrentUser, request: Request) -> dict[str, Any]:
    """Reconcile CH quota tiers + service roles + per-org row policies into
    ClickHouse, minting the service-user secrets via the secrets seam. Idempotent.
    governance:write.
    """
    from dfe_engine.clickhouse.clickhouse_manager import ClickHouseManager
    from dfe_engine.governance.ch import reconcile_ch_rbac
    from dfe_engine.secrets import build_secrets
    from dfe_engine.settings import load_settings

    settings = load_settings()
    ch_cfg = {
        "ch_host": settings.clickhouse.host,
        "ch_port": settings.clickhouse.port,
        "ch_username": settings.clickhouse.username,
        "ch_password": settings.clickhouse.password,
        "ch_secure": settings.clickhouse.secure,
        "ch_verify": settings.clickhouse.verify,
    }
    try:
        admin_client = ClickHouseManager.get_instance(ch_cfg).get_clickhouse_client()._client
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "clickhouse_unavailable", "message": str(exc)},
        ) from exc

    org_registry = getattr(request.app.state, "org_registry", None)
    orgs = org_registry.list() if org_registry is not None else []
    result = reconcile_ch_rbac(
        admin_client,
        secrets_store=build_secrets(settings.secrets),
        orgs=orgs,
    )
    return {
        "statements": len(result.statements),
        "dropped": len(result.dropped),
        "minted": result.minted,
        "errors": result.errors,
    }
