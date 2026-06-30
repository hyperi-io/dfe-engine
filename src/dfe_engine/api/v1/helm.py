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
from dfe_engine.gitcrud import ConcurrencyConflictError, GitCrud
from dfe_engine.gitcrud.commit_policy import (
    CommitContext,
    CommitPolicyError,
    build_message,
    validate_change,
)
from dfe_engine.governance import PolicyStore, ProtectedVarError

router = APIRouter(prefix="/helm", tags=["Governed Ops: Helm Vars"])

_CLASS = "helmvars"


class SetVarRequest(BaseModel):
    value: Any


class WriteResult(BaseModel):
    changed: bool
    commit_sha: str | None = None


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


@router.get("/files", dependencies=[Depends(require_action("helmvars:read"))])
async def list_files(user: CurrentUser, request: Request) -> list[str]:
    """List helm-var overlay resources."""
    return _gitcrud(request).list(_CLASS)


@router.get("/files/{name}/vars", dependencies=[Depends(require_action("helmvars:read"))])
async def list_vars(name: str, user: CurrentUser, request: Request) -> list[dict[str, Any]]:
    """Flattened dot-path vars for a resource, each marked protected or not."""
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
    dependencies=[Depends(require_action("helmvars:write"))],
)
async def set_var(
    name: str,
    path: str,
    body: SetVarRequest,
    user: CurrentUser,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> WriteResult:
    """Set a helm var (-> gitops commit). 409 on stale If-Match; 403 if protected."""
    gc = _gitcrud(request)
    policy = _policy(request)

    try:
        validate_change(path, body.value)
    except CommitPolicyError as exc:
        raise HTTPException(403, detail={"code": "policy_violation", "message": str(exc)}) from exc

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
    try:
        res = gc.set_key(
            _CLASS, name, path, body.value, user.user_id, message=msg, base_revision=if_match
        )
    except ConcurrencyConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "conflict",
                "message": str(exc),
                "context": {"current": exc.current, "head": exc.head},
            },
        ) from exc
    audit_resource_change(
        user.user_id,
        "helmvars",
        name,
        "updated",
        {"path": path, "commit": res.commit_sha},
    )
    return WriteResult(changed=res.changed, commit_sha=res.commit_sha)


@router.delete(
    "/files/{name}/vars/{path}",
    response_model=WriteResult,
    dependencies=[Depends(require_action("helmvars:write"))],
)
async def delete_var(name: str, path: str, user: CurrentUser, request: Request) -> WriteResult:
    """Revert a helm var to its chart default (remove the override)."""
    gc = _gitcrud(request)
    res = gc.delete_key(_CLASS, name, path, user.user_id)
    audit_resource_change(user.user_id, "helmvars", name, "updated", {"path": path, "revert": True})
    return WriteResult(changed=res.changed, commit_sha=res.commit_sha)
