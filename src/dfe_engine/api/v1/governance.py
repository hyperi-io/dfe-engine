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

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel

from dfe_engine.api.deps import CurrentUser, require_action
from dfe_engine.auth.audit import audit_resource_change
from dfe_engine.auth.engine import authorize
from dfe_engine.gitcrud import GitCrud, ResourceNotFoundError
from dfe_engine.gitcrud.commit_policy import CommitPolicyError, validate_name
from dfe_engine.gitcrud.routing import ReviewRequiredError, WriteOutcome, route_write
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
    # Set when a production+team invoke was routed to a review PR instead of main.
    review_required: bool = False
    pr_url: str | None = None
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


def _forge(request: Request):
    """The deploy-repo forge client for opening review PRs (None -> refuse)."""
    return getattr(request.app.state, "forge", None)


def _check_name(name: str) -> None:
    """400 on a resource name that could traverse the tree or forge a trailer."""
    try:
        validate_name(name)
    except CommitPolicyError as exc:
        raise HTTPException(
            status_code=400, detail={"code": "invalid_name", "message": str(exc)}
        ) from exc


def _route_admin_write(
    request: Request, *, resource: str, actor: str, title: str, body: str, write
) -> WriteOutcome:
    """route_write for a governance-class admin CRUD write (201/204 endpoints).

    Direct commit in dev/solo; a production+team write is pushed to a review branch
    and a PR opened (or 409 ``review_required`` when no forge is configured). The
    caller surfaces any PR via response headers since these bodies are fixed.
    """
    settings = request.app.state.settings
    try:
        return route_write(
            gc=_gitcrud(request),
            forge=_forge(request),
            environment=settings.env,
            mode=settings.gitops.mode,
            rbac_class="governance",
            resource=resource,
            actor=actor,
            title=title,
            body=body,
            write=write,
        )
    except ReviewRequiredError as exc:
        raise HTTPException(
            status_code=409, detail={"code": "review_required", "message": str(exc)}
        ) from exc


def _apply_review_headers(response: Response, outcome: WriteOutcome) -> None:
    """Signal a routed-to-PR admin write on the fixed-body 201/204 responses."""
    if outcome.review_required:
        response.headers["X-DFE-Review-Required"] = "true"
        if outcome.pr_url:
            response.headers["X-DFE-PR-Url"] = outcome.pr_url


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
    """Invoke a defined action - gated on the action's OWN required_action.

    Direct commit in dev/solo; a production+team invoke is routed to a review PR
    (or 409 ``review_required`` when no forge is configured).
    """
    _check_name(name)
    store = _actions(request)
    settings = request.app.state.settings
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

    # Validate + build the diff via a dry run first: this surfaces protected-var /
    # forbidden / policy violations as 403 BEFORE any review-vs-direct routing, so
    # a bad action never opens a PR (and 403 keeps precedence over 409).
    try:
        preview = store.invoke(name, user.user_id, policy=policy, dry_run=True, override=override)
    except ProtectedVarError as exc:
        raise HTTPException(403, detail={"code": "protected_var", "message": str(exc)}) from exc
    except ActionForbiddenError as exc:
        raise HTTPException(403, detail={"code": "action_forbidden", "message": str(exc)}) from exc
    except CommitPolicyError as exc:
        raise HTTPException(403, detail={"code": "policy_violation", "message": str(exc)}) from exc

    if dry_run:
        return InvokeResponse(dry_run=True, changed=False, commit_sha=None, diff=preview.diff)

    def _write(branch: str):
        return store.invoke(
            name, user.user_id, policy=policy, dry_run=False, override=override, branch=branch
        )

    try:
        outcome = route_write(
            gc=_gitcrud(request),
            forge=_forge(request),
            environment=settings.env,
            mode=settings.gitops.mode,
            rbac_class="governance",
            resource=f"action/{name}",
            actor=user.user_id,
            title=f"action({name}): invoke",
            body=f"Invoke action '{name}' by {user.user_id}. Opened for review "
            "because production+team may not commit straight to main.",
            write=_write,
        )
    except ReviewRequiredError as exc:
        raise HTTPException(
            status_code=409, detail={"code": "review_required", "message": str(exc)}
        ) from exc
    audit_resource_change(
        user.user_id,
        "action",
        name,
        "invoked",
        {"commit": outcome.commit_sha, "pr": outcome.pr_url},
    )
    return InvokeResponse(
        dry_run=False,
        changed=outcome.changed,
        commit_sha=outcome.commit_sha,
        auto_merged=outcome.auto_merged,
        review_required=outcome.review_required,
        pr_url=outcome.pr_url,
        diff=preview.diff,
    )


@router.post(
    "/admin/actions",
    status_code=201,
    dependencies=[Depends(require_action("governance:write"))],
)
async def create_action(
    body: ActionDef, user: CurrentUser, request: Request, response: Response
) -> ActionDef:
    _check_name(body.name)

    def _write(branch: str):
        return _actions(request).save(body, user.user_id, branch=branch)

    outcome = _route_admin_write(
        request,
        resource=f"action/{body.name}",
        actor=user.user_id,
        title=f"action({body.name}): define",
        body=f"Define action '{body.name}' by {user.user_id}. Opened for review "
        "because production+team may not commit straight to main.",
        write=_write,
    )
    audit_resource_change(user.user_id, "action", body.name, "created")
    _apply_review_headers(response, outcome)
    return body


@router.delete(
    "/admin/actions/{name}",
    status_code=204,
    dependencies=[Depends(require_action("governance:write"))],
)
async def delete_action(name: str, user: CurrentUser, request: Request, response: Response) -> None:
    _check_name(name)
    # Surface 404 before any review routing.
    try:
        _actions(request).get(name)
    except ResourceNotFoundError as exc:
        raise HTTPException(404, detail={"code": "not_found", "message": str(exc)}) from exc

    def _write(branch: str):
        return _actions(request).delete(name, user.user_id, branch=branch)

    outcome = _route_admin_write(
        request,
        resource=f"action/{name}",
        actor=user.user_id,
        title=f"action({name}): delete",
        body=f"Delete action '{name}' by {user.user_id}. Opened for review "
        "because production+team may not commit straight to main.",
        write=_write,
    )
    audit_resource_change(user.user_id, "action", name, "deleted")
    _apply_review_headers(response, outcome)


@router.post(
    "/admin/policies",
    status_code=201,
    dependencies=[Depends(require_action("governance:write"))],
)
async def create_policy(
    body: ProtectedPolicy, user: CurrentUser, request: Request, response: Response
) -> ProtectedPolicy:
    _check_name(body.name)
    gc = _gitcrud(request)

    def _write(branch: str):
        return gc.put(_POLICY_CLASS, body.name, body.model_dump(), user.user_id, branch=branch)

    outcome = _route_admin_write(
        request,
        resource=f"policy/{body.name}",
        actor=user.user_id,
        title=f"rbac({body.name}): define protected-var policy",
        body=f"Define protected-var policy '{body.name}' by {user.user_id}. Opened "
        "for review because production+team may not commit straight to main.",
        write=_write,
    )
    audit_resource_change(user.user_id, "policy", body.name, "created")
    _apply_review_headers(response, outcome)
    return body


@router.delete(
    "/admin/policies/{name}",
    status_code=204,
    dependencies=[Depends(require_action("governance:write"))],
)
async def delete_policy(name: str, user: CurrentUser, request: Request, response: Response) -> None:
    _check_name(name)
    gc = _gitcrud(request)
    # Surface 404 before any review routing.
    try:
        gc.get(_POLICY_CLASS, name)
    except ResourceNotFoundError as exc:
        raise HTTPException(404, detail={"code": "not_found", "message": str(exc)}) from exc

    def _write(branch: str):
        return gc.delete(_POLICY_CLASS, name, user.user_id, branch=branch)

    outcome = _route_admin_write(
        request,
        resource=f"policy/{name}",
        actor=user.user_id,
        title=f"rbac({name}): delete protected-var policy",
        body=f"Delete protected-var policy '{name}' by {user.user_id}. Opened for "
        "review because production+team may not commit straight to main.",
        write=_write,
    )
    audit_resource_change(user.user_id, "policy", name, "deleted")
    _apply_review_headers(response, outcome)


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
        "ch_ca_cert": settings.clickhouse.ca_cert,
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
