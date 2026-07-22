#  Project:      dfe-engine
#  File:         api/v1/helm.py
#  Purpose:      Governed Ops Tier-1 - generic helm-var CRUD over gitops
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tier-1 generic helm-var CRUD (admin-grade).

GET    /api/v1/helm/files                  -> list overlay resources
GET    /api/v1/helm/files/{name}/vars      -> flattened dot-path vars (+ protected)
PUT    /api/v1/helm/files/{name}/vars/{path}   -> set a var (-> gitops commit)
DELETE /api/v1/helm/files/{name}/vars/{path}   -> revert a var to default

Every mutation ends in a git commit via the engine (no live cluster writes). RBAC
is bound at the class: helmvars:read / helmvars:write (+ helmvars:override for
protected vars). Optimistic concurrency via the If-Match header (commit SHA).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel

from dfe_engine.api.deps import CurrentUser, require_action
from dfe_engine.auth.audit import audit_resource_change
from dfe_engine.auth.engine import authorize
from dfe_engine.auth.rbac_scopes import scopes_dict
from dfe_engine.gitcrud import ConcurrencyConflictError, GitCrud
from dfe_engine.gitcrud.commit_policy import (
    CommitContext,
    CommitPolicyError,
    build_message,
    validate_change,
    validate_name,
)
from dfe_engine.gitcrud.routing import ReviewRequiredError, route_write
from dfe_engine.governance import PolicyStore, ProtectedVarError

router = APIRouter(prefix="/helm", tags=["Governed Ops: Helm Vars"])

_CLASS = "helmvars"


class SetVarRequest(BaseModel):
    value: Any


class WriteResult(BaseModel):
    changed: bool
    commit_sha: str | None = None
    auto_merged: bool = False
    # Set when a production+team write was routed to a review PR instead of main.
    review_required: bool = False
    pr_url: str | None = None


def _gitcrud(request: Request) -> GitCrud:
    gc = getattr(request.app.state, "gitcrud", None)
    if gc is None:
        raise HTTPException(
            status_code=503,
            detail={"code": "not_configured", "message": "gitops is not enabled"},
        )
    return gc


def _policy(request: Request) -> PolicyStore | None:
    return getattr(request.app.state, "policy_store", None)


def _has_override(request: Request, user: Any) -> bool:
    return authorize(user, "helmvars:override", role_config=request.app.state.role_config).allowed


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


@router.get("/files", dependencies=[Depends(require_action(scopes_dict["helmvars_read"]))])
async def list_files(user: CurrentUser, request: Request) -> list[str]:
    """List helm-var overlay resources."""
    return _gitcrud(request).list(_CLASS)


@router.get(
    "/files/{name}/vars", dependencies=[Depends(require_action(scopes_dict["helmvars_read"]))]
)
async def list_vars(name: str, user: CurrentUser, request: Request) -> list[dict[str, Any]]:
    """Flattened dot-path vars for a resource, each marked protected or not."""
    _check_name(name)
    gc = _gitcrud(request)
    policy = _policy(request)
    out: list[dict[str, Any]] = []
    for path, value in gc.vars(_CLASS, name).items():
        protected = bool(policy and policy.is_protected(_CLASS, name, path))
        out.append({"path": path, "value": value, "protected": protected})
    return out


@router.put(
    "/files/{name}/vars/{path}",
    response_model=WriteResult,
    dependencies=[Depends(require_action(scopes_dict["helmvars_write"]))],
)
async def set_var(
    name: str,
    path: str,
    body: SetVarRequest,
    user: CurrentUser,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> WriteResult:
    """Set a helm var. Direct commit in dev/solo; routed to a review PR (or 409
    'review_required') in production+team. 409 on stale If-Match; 403 if protected.
    """
    _check_name(name)
    gc = _gitcrud(request)
    policy = _policy(request)
    settings = request.app.state.settings

    try:
        validate_change(path, body.value)
    except CommitPolicyError as exc:
        raise HTTPException(403, detail={"code": "policy_violation", "message": str(exc)}) from exc

    protected = bool(policy and policy.is_protected(_CLASS, name, path))
    if policy is not None:
        try:
            policy.enforce(_CLASS, name, path, override=_has_override(request, user))
        except ProtectedVarError as exc:
            raise HTTPException(403, detail={"code": "protected_var", "message": str(exc)}) from exc

    msg = build_message(
        CommitContext(
            ctype="cfg",
            scope=name,
            summary=f"set {path.split('.')[-1]}"[:40],
            actor=user.user_id,
            role="helmvars:write",
            base_revision=if_match or "",
        )
    )

    def _write(branch: str):
        return gc.set_key(
            _CLASS,
            name,
            path,
            body.value,
            user.user_id,
            message=msg,
            base_revision=if_match,
            branch=branch,
        )

    try:
        outcome = route_write(
            gc=gc,
            forge=_forge(request),
            environment=settings.env,
            mode=settings.gitops.mode,
            rbac_class=_CLASS,
            resource=f"{_CLASS}/{name}:{path}",
            actor=user.user_id,
            protected=protected,
            title=f"cfg({name}): set {path}",
            body=f"Governed helm-var change to {name} ({path}={body.value!r}) "
            f"by {user.user_id}. Opened for review because production+team may "
            "not commit straight to main.",
            write=_write,
        )
    except ConcurrencyConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "conflict",
                "message": str(exc),
                # non-reserved keys become the ErrorResponse.context (current vs theirs)
                "current": exc.current,
                "head": exc.head,
            },
        ) from exc
    except ReviewRequiredError as exc:
        raise HTTPException(
            status_code=409, detail={"code": "review_required", "message": str(exc)}
        ) from exc
    audit_resource_change(
        user.user_id,
        "helmvars",
        name,
        "updated",
        {"path": path, "commit": outcome.commit_sha, "pr": outcome.pr_url},
    )
    return WriteResult(
        changed=outcome.changed,
        commit_sha=outcome.commit_sha,
        auto_merged=outcome.auto_merged,
        review_required=outcome.review_required,
        pr_url=outcome.pr_url,
    )


@router.delete(
    "/files/{name}/vars/{path}",
    response_model=WriteResult,
    dependencies=[Depends(require_action(scopes_dict["helmvars_write"]))],
)
async def delete_var(name: str, path: str, user: CurrentUser, request: Request) -> WriteResult:
    """Revert a helm var to its chart default. Routed like set_var (PR in prod+team)."""
    _check_name(name)
    gc = _gitcrud(request)
    settings = request.app.state.settings

    def _write(branch: str):
        return gc.delete_key(_CLASS, name, path, user.user_id, branch=branch)

    try:
        outcome = route_write(
            gc=gc,
            forge=_forge(request),
            environment=settings.env,
            mode=settings.gitops.mode,
            rbac_class=_CLASS,
            resource=f"{_CLASS}/{name}:{path}",
            actor=user.user_id,
            title=f"cfg({name}): revert {path}",
            body=f"Revert helm-var {name} ({path}) by {user.user_id}. Opened for "
            "review because production+team may not commit straight to main.",
            write=_write,
        )
    except ReviewRequiredError as exc:
        raise HTTPException(
            status_code=409, detail={"code": "review_required", "message": str(exc)}
        ) from exc
    audit_resource_change(user.user_id, "helmvars", name, "updated", {"path": path, "revert": True})
    return WriteResult(
        changed=outcome.changed,
        commit_sha=outcome.commit_sha,
        auto_merged=outcome.auto_merged,
        review_required=outcome.review_required,
        pr_url=outcome.pr_url,
    )
