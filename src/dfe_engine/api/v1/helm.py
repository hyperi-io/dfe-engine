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


def gitcrud_of(request: Request) -> GitCrud:
    gc = getattr(request.app.state, "gitcrud", None)
    if gc is None:
        raise HTTPException(
            status_code=503,
            detail={"code": "not_configured", "message": "gitops is not enabled"},
        )
    return gc


def policy_of(request: Request) -> PolicyStore | None:
    return getattr(request.app.state, "policy_store", None)


def has_override(request: Request, user: Any) -> bool:
    return authorize(user, "helmvars:override", role_config=request.app.state.role_config).allowed


def forge_of(request: Request):
    """The deploy-repo forge client for opening review PRs (None -> refuse)."""
    return getattr(request.app.state, "forge", None)


def check_name(name: str) -> None:
    """400 on a resource name that could traverse the tree or forge a trailer."""
    try:
        validate_name(name)
    except CommitPolicyError as exc:
        raise HTTPException(
            status_code=400, detail={"code": "invalid_name", "message": str(exc)}
        ) from exc


def enforce_protected(request: Request, user: Any, cls: str, name: str, path: str) -> bool:
    """403 unless the caller may write this var. Returns whether it is protected."""
    policy = policy_of(request)
    if policy is None:
        return False
    protected = policy.is_protected(cls, name, path)
    try:
        policy.enforce(cls, name, path, override=has_override(request, user))
    except ProtectedVarError as exc:
        raise HTTPException(403, detail={"code": "protected_var", "message": str(exc)}) from exc
    return protected


def set_var_governed(
    cls: str,
    name: str,
    path: str,
    value: Any,
    user: Any,
    request: Request,
    if_match: str | None,
) -> WriteResult:
    """Set one var in one resource class: policy, routing, commit, audit.

    Shared by every governed-var surface, so a new class binds to the same
    guarantees rather than reimplementing them one check short.
    """
    check_name(name)
    gc = gitcrud_of(request)
    settings = request.app.state.settings

    try:
        validate_change(path, value)
    except CommitPolicyError as exc:
        raise HTTPException(403, detail={"code": "policy_violation", "message": str(exc)}) from exc

    protected = enforce_protected(request, user, cls, name, path)

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
            cls, name, path, value, user.user_id, message=msg, base_revision=if_match, branch=branch
        )

    try:
        outcome = route_write(
            gc=gc,
            forge=forge_of(request),
            environment=settings.env,
            mode=settings.gitops.mode,
            rbac_class=cls,
            resource=f"{cls}/{name}:{path}",
            actor=user.user_id,
            protected=protected,
            title=f"cfg({name}): set {path}",
            body=f"Governed helm-var change to {name} ({path}={value!r}) "
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
        cls,
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


def delete_var_governed(cls: str, name: str, path: str, user: Any, request: Request) -> WriteResult:
    """Revert one var to its chart default. Reverting a protected var IS changing
    it, so the same override grant applies as on a set."""
    check_name(name)
    gc = gitcrud_of(request)
    settings = request.app.state.settings
    protected = enforce_protected(request, user, cls, name, path)

    def _write(branch: str):
        return gc.delete_key(cls, name, path, user.user_id, branch=branch)

    try:
        outcome = route_write(
            gc=gc,
            forge=forge_of(request),
            environment=settings.env,
            mode=settings.gitops.mode,
            rbac_class=cls,
            resource=f"{cls}/{name}:{path}",
            actor=user.user_id,
            protected=protected,
            title=f"cfg({name}): revert {path}",
            body=f"Revert helm-var {name} ({path}) by {user.user_id}. Opened for "
            "review because production+team may not commit straight to main.",
            write=_write,
        )
    except ReviewRequiredError as exc:
        raise HTTPException(
            status_code=409, detail={"code": "review_required", "message": str(exc)}
        ) from exc
    audit_resource_change(user.user_id, cls, name, "updated", {"path": path, "revert": True})
    return WriteResult(
        changed=outcome.changed,
        commit_sha=outcome.commit_sha,
        auto_merged=outcome.auto_merged,
        review_required=outcome.review_required,
        pr_url=outcome.pr_url,
    )


@router.get("/files", dependencies=[Depends(require_action(scopes_dict["helmvars_read"]))])
async def list_files(user: CurrentUser, request: Request) -> list[str]:
    """List helm-var overlay resources."""
    return gitcrud_of(request).list(_CLASS)


@router.get(
    "/files/{name}/vars", dependencies=[Depends(require_action(scopes_dict["helmvars_read"]))]
)
async def list_vars(name: str, user: CurrentUser, request: Request) -> list[dict[str, Any]]:
    """Flattened dot-path vars for a resource, each marked protected or not."""
    check_name(name)
    gc = gitcrud_of(request)
    policy = policy_of(request)
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
    return set_var_governed(_CLASS, name, path, body.value, user, request, if_match)


@router.delete(
    "/files/{name}/vars/{path}",
    response_model=WriteResult,
    dependencies=[Depends(require_action(scopes_dict["helmvars_write"]))],
)
async def delete_var(name: str, path: str, user: CurrentUser, request: Request) -> WriteResult:
    """Revert a helm var to its chart default. Routed like set_var (PR in prod+team);
    403 if protected, since reverting a locked var changes it as surely as setting it.
    """
    return delete_var_governed(_CLASS, name, path, user, request)
