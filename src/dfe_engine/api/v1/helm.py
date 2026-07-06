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

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel

from dfe_engine.api.deps import CurrentUser, require_action
from dfe_engine.auth.audit import audit_resource_change
from dfe_engine.auth.engine import authorize
from dfe_engine.gitcrud import ConcurrencyConflictError, GitCrud, ResourceNotFoundError
from dfe_engine.gitcrud.auto_merge import apply_auto_merge, resolve_state
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
    auto_merged: bool = False


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


def _enforce_protected(request: Request, name: str, path: str, user: Any) -> bool:
    """Protected-var gate shared by set AND delete; returns the protected flag.

    DELETE reverts a var to its chart default - that is still a WRITE to a
    protected var, so it must clear the SAME policy.enforce bar as PUT. Without
    this a helmvars:write holder could erase a locked override via DELETE without
    the helmvars:override grant PUT requires. Raises 403 protected_var when locked
    and no override is held.
    """
    policy = _policy(request)
    protected = bool(policy and policy.is_protected(_CLASS, name, path))
    if policy is not None:
        try:
            policy.enforce(_CLASS, name, path, override=_has_override(request, user))
        except ProtectedVarError as exc:
            raise HTTPException(403, detail={"code": "protected_var", "message": str(exc)}) from exc
    return protected


def _auto_merged(
    request: Request,
    gc: GitCrud,
    *,
    protected: bool,
    actor: str,
    resource: str,
    changed: bool,
    commit_sha: str | None,
) -> bool:
    """Badge + WARN when auto-merge converted this write from PR-mode to direct.

    A no-op write (no commit) is never badged and never WARNs - mirrors
    governance.py's ``if not dry_run and res.changed`` guard.
    """
    if not changed:
        return False
    settings = request.app.state.settings
    state = resolve_state(gc, environment=settings.env, mode=settings.gitops.mode)
    return apply_auto_merge(
        state,
        environment=settings.env,
        rbac_class=_CLASS,
        protected=protected,
        actor=actor,
        resource=resource,
        commit_sha=commit_sha,
    )


@router.get("/files", dependencies=[Depends(require_action("helmvars:read"))])
async def list_files(user: CurrentUser, request: Request) -> list[str]:
    """List helm-var overlay resources."""
    return _gitcrud(request).list(_CLASS)


@router.get("/files/{name}/vars", dependencies=[Depends(require_action("helmvars:read"))])
async def list_vars(name: str, user: CurrentUser, request: Request) -> list[dict[str, Any]]:
    """Flattened dot-path vars for a resource, each marked protected or not."""
    gc = _gitcrud(request)
    policy = _policy(request)
    try:
        flat = gc.vars(_CLASS, name)
    except ResourceNotFoundError as exc:
        raise HTTPException(404, detail={"code": "not_found", "message": str(exc)}) from exc
    out: list[dict[str, Any]] = []
    for path, value in flat.items():
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

    try:
        validate_change(path, body.value)
    except CommitPolicyError as exc:
        raise HTTPException(403, detail={"code": "policy_violation", "message": str(exc)}) from exc

    protected = _enforce_protected(request, name, path, user)

    # build_message budget-truncates an over-long subject; a residual violation
    # (non-ASCII / bad type) is a client-fixable 422, never an unhandled 500.
    try:
        msg = build_message(
            CommitContext(
                ctype="cfg",
                scope=name,
                summary=f"set {path.split('.')[-1]}",
                actor=user.user_id,
                role="helmvars:write",
                base_revision=if_match or "",
            )
        )
    except CommitPolicyError as exc:
        raise HTTPException(
            422, detail={"code": "invalid_commit_message", "message": str(exc)}
        ) from exc
    try:
        res = await asyncio.to_thread(
            gc.set_key,
            _CLASS,
            name,
            path,
            body.value,
            user.user_id,
            message=msg,
            base_revision=if_match,
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
    except ValueError as exc:
        # a bad list-path index (name[i] out of range / into a non-list) raises
        # ValueError from the dot-path walker - a client path error, so 422.
        raise HTTPException(422, detail={"code": "invalid_path", "message": str(exc)}) from exc
    audit_resource_change(
        user.user_id,
        "helmvars",
        name,
        "updated",
        {"path": path, "commit": res.commit_sha},
    )
    return WriteResult(
        changed=res.changed,
        commit_sha=res.commit_sha,
        auto_merged=_auto_merged(
            request,
            gc,
            protected=protected,
            actor=user.user_id,
            resource=f"{_CLASS}/{name}:{path}",
            changed=res.changed,
            commit_sha=res.commit_sha,
        ),
    )


@router.delete(
    "/files/{name}/vars/{path}",
    response_model=WriteResult,
    dependencies=[Depends(require_action("helmvars:write"))],
)
async def delete_var(
    name: str,
    path: str,
    user: CurrentUser,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> WriteResult:
    """Revert a helm var to its chart default (remove the override).

    Same guardrails as set_var: a protected var needs helmvars:override to revert,
    the If-Match concurrency check applies, and the badge carries the real
    protected flag (a revert is a write, not a free pass).
    """
    gc = _gitcrud(request)
    protected = _enforce_protected(request, name, path, user)
    try:
        res = await asyncio.to_thread(
            gc.delete_key, _CLASS, name, path, user.user_id, base_revision=if_match
        )
    except ResourceNotFoundError as exc:
        raise HTTPException(404, detail={"code": "not_found", "message": str(exc)}) from exc
    except ValueError as exc:
        # ValueError from the dot-path walker - a client path error, so 422 (parity
        # with set_var/get_var; without this an invalid path 500s here). (P3.7)
        raise HTTPException(422, detail={"code": "invalid_path", "message": str(exc)}) from exc
    except ConcurrencyConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "conflict",
                "message": str(exc),
                "current": exc.current,
                "head": exc.head,
            },
        ) from exc
    audit_resource_change(user.user_id, "helmvars", name, "updated", {"path": path, "revert": True})
    return WriteResult(
        changed=res.changed,
        commit_sha=res.commit_sha,
        auto_merged=_auto_merged(
            request,
            gc,
            protected=protected,
            actor=user.user_id,
            resource=f"{_CLASS}/{name}:{path}",
            changed=res.changed,
            commit_sha=res.commit_sha,
        ),
    )
