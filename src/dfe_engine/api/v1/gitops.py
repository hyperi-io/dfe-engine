#  Project:      dfe-engine
#  File:         api/v1/gitops.py
#  Purpose:      Governed Ops - engine-wide gitops settings (auto-merge)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Auto-merge status + toggle (the dfe-ui banner contract), audit log, and the
resource-class enumeration the generated clients build their selects from.

GET /api/v1/gitops/auto-merge -> {stored, effective, allowed, reason} (governance:read)
PUT /api/v1/gitops/auto-merge -> toggle; refuses to enable when the deployment
gate (dev posture OR DFE_GITOPS_MODE=solo) does not permit (governance:write).
GET /api/v1/gitops/log -> flat {entries, next_before} or, with ?group_by=,
{groups} - every gitcrud commit, newest-first (governance:read).
GET /api/v1/gitops/classes -> registry dump (any authenticated caller)
GET /api/v1/gitops/classes/{cls}/resources -> names ({cls}'s own :read grant)
GET /api/v1/gitops/classes/{cls}/resources/{name}/vars -> dot-path vars (ditto)

The enumeration trio exists for the contract rule: if the server will reject
values outside a set, the contract must expose the set. VarChange's fields
carry ``x-dfe-enum-source`` annotations pointing here.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from scalo.logger import logger

from dfe_engine.api.deps import CurrentUser, require_action
from dfe_engine.api.write_turn import WRITE_TURN
from dfe_engine.auth.audit import audit_resource_change
from dfe_engine.auth.engine import authorize
from dfe_engine.auth.models import AuthContext
from dfe_engine.auth.rbac_scopes import scopes_dict
from dfe_engine.gitcrud import GitCrud, ResourceNotFoundError
from dfe_engine.gitcrud.auto_merge import AutoMergeState, resolve_state, set_stored
from dfe_engine.gitcrud.commit_policy import CommitPolicyError, validate_name
from dfe_engine.gitcrud.log import LogEntry, UnknownCursorError, group_log, read_log
from dfe_engine.gitcrud.models import ResourceClass
from dfe_engine.gitcrud.registry import UnknownResourceClassError

router = APIRouter(prefix="/gitops", tags=["Governed Ops: Gitops"], dependencies=[WRITE_TURN])


class AutoMergeStatus(BaseModel):
    stored: bool
    effective: bool
    allowed: bool
    reason: str


class AutoMergeRequest(BaseModel):
    enabled: bool


def _gitcrud(request: Request) -> GitCrud:
    gc = getattr(request.app.state, "gitcrud", None)
    if gc is None:
        raise HTTPException(
            status_code=503,
            detail={"code": "not_configured", "message": "gitops is not enabled"},
        )
    return gc


def _state(request: Request, *, warn: bool = True) -> AutoMergeState:
    settings = request.app.state.settings
    return resolve_state(
        _gitcrud(request), environment=settings.env, mode=settings.gitops.mode, warn=warn
    )


def _status(state: AutoMergeState) -> AutoMergeStatus:
    return AutoMergeStatus(
        stored=state.stored,
        effective=state.effective,
        allowed=state.allowed,
        reason=state.reason,
    )


@router.get(
    "/auto-merge",
    response_model=AutoMergeStatus,
    dependencies=[Depends(require_action(scopes_dict["governance_read"]))],
)
def get_auto_merge(user: CurrentUser, request: Request) -> AutoMergeStatus:
    """Auto-merge status for the UI banner: stored flag, gate verdict, net effect."""
    # warn=False: the UI polls this - a stranded flag must not WARN per poll.
    return _status(_state(request, warn=False))


@router.put(
    "/auto-merge",
    response_model=AutoMergeStatus,
    dependencies=[Depends(require_action(scopes_dict["governance_write"]))],
)
def put_auto_merge(body: AutoMergeRequest, user: CurrentUser, request: Request) -> AutoMergeStatus:
    """Toggle auto-merge. Enabling requires the deployment gate; disabling always works."""
    gc = _gitcrud(request)
    # warn=False both calls: the stranded-flag case on enable is already refused
    # below with a 403 that carries the reason - no need to also WARN here.
    state = _state(request, warn=False)
    if body.enabled and not state.allowed:
        raise HTTPException(403, detail={"code": "auto_merge_forbidden", "message": state.reason})
    if body.enabled:
        logger.warning("AUTO-MERGE enabled via API", actor=user.user_id)
    set_stored(gc, body.enabled, user.user_id)
    audit_resource_change(
        user.user_id, "gitops", "auto-merge", "updated", {"enabled": body.enabled}
    )
    return _status(_state(request, warn=False))


class ClassInfo(BaseModel):
    """One registered resource class - the closed set behind VarChange.cls.

    action_writable exports the escalation guard: a defined action may never
    change a governance-prefixed class, so a UI can filter its class select
    to the legal targets instead of discovering the ban at 403-time.
    """

    name: str
    rbac_prefix: str
    directory: str
    versioned: bool
    action_writable: bool


class VarEntry(BaseModel):
    """One flattened dot-path var in a resource doc."""

    path: str
    value: Any = None
    protected: bool


def _resource_class(gc: GitCrud, cls: str) -> ResourceClass:
    try:
        return gc.registry.get(cls)
    except UnknownResourceClassError as exc:
        raise HTTPException(
            404, detail={"code": "unknown_class", "message": f"unknown resource class '{cls}'"}
        ) from exc


def _require_class_read(request: Request, user: AuthContext, rc: ResourceClass) -> None:
    """Gate on the CLASS's own :read grant - never a blanket one."""
    decision = authorize(user, rc.action("read"), role_config=request.app.state.role_config)
    if not decision.allowed:
        raise HTTPException(403, detail={"code": "forbidden", "message": decision.reason})


@router.get("/classes", response_model=list[ClassInfo])
def list_classes(user: CurrentUser, request: Request) -> list[ClassInfo]:
    """The resource-class registry - what VarChange.cls may legally name.

    Registry metadata only (no resource content), so any authenticated caller
    may read it; the per-class listings below are gated by each class's grant.
    """
    gc = _gitcrud(request)
    return [
        ClassInfo(
            name=rc.name,
            rbac_prefix=rc.rbac_prefix or rc.name,
            directory=rc.directory,
            versioned=rc.versioned,
            action_writable=(rc.rbac_prefix or rc.name) != "governance",
        )
        for rc in gc.registry.all()
    ]


@router.get("/classes/{cls}/resources", response_model=list[str])
def list_class_resources(cls: str, user: CurrentUser, request: Request) -> list[str]:
    """Resource names in a class - what VarChange.name may legally name."""
    gc = _gitcrud(request)
    rc = _resource_class(gc, cls)
    _require_class_read(request, user, rc)
    return gc.list(cls)


@router.get("/classes/{cls}/resources/{name}/vars", response_model=list[VarEntry])
def list_class_resource_vars(
    cls: str, name: str, user: CurrentUser, request: Request
) -> list[VarEntry]:
    """Flattened dot-path vars of one resource - what VarChange.path may name.

    Values ride along so a select can show the current value beside each path.
    """
    gc = _gitcrud(request)
    rc = _resource_class(gc, cls)
    _require_class_read(request, user, rc)
    try:
        validate_name(name)
    except CommitPolicyError as exc:
        raise HTTPException(400, detail={"code": "invalid_name", "message": str(exc)}) from exc
    policy = getattr(request.app.state, "policy_store", None)
    try:
        flat = gc.vars(cls, name)
    except ResourceNotFoundError as exc:
        raise HTTPException(404, detail={"code": "not_found", "message": str(exc)}) from exc
    return [
        VarEntry(
            path=path,
            value=value,
            protected=bool(policy and policy.is_protected(cls, name, path)),
        )
        for path, value in flat.items()
    ]


class LogEntryModel(BaseModel):
    sha: str
    timestamp: int
    ctype: str
    scope: str
    summary: str
    actor: str
    role: str
    action: str
    request_id: str
    files: list[str]
    resources: list[str]
    conforming: bool
    state: str


class LogResponse(BaseModel):
    entries: list[LogEntryModel]
    next_before: str | None = None


class LogGroup(BaseModel):
    key: str
    count: int
    latest: LogEntryModel


class GroupedLogResponse(BaseModel):
    groups: list[LogGroup]


def _entry_model(e: LogEntry) -> LogEntryModel:
    return LogEntryModel(
        sha=e.sha,
        timestamp=e.timestamp,
        ctype=e.ctype,
        scope=e.scope,
        summary=e.summary,
        actor=e.actor,
        role=e.role,
        action=e.action,
        request_id=e.request_id,
        files=e.files,
        resources=e.resources,
        conforming=e.conforming,
        state=e.state,
    )


@router.get(
    "/log",
    response_model=LogResponse | GroupedLogResponse,
    dependencies=[Depends(require_action(scopes_dict["governance_read"]))],
)
def get_log(
    user: CurrentUser,
    request: Request,
    limit: int = Query(default=50, ge=1, le=500),
    before: str | None = Query(default=None),
    group_by: str | None = Query(default=None, pattern="^(scope|actor|type|day)$"),
    applied_revision: str | None = Query(default=None),
) -> LogResponse | GroupedLogResponse:
    """Gitcrud audit log: every governed-ops git change, newest-first.

    Flat + cursor-paginated by default; ?group_by= buckets the page for
    summaries. applied_revision (the Argo-synced SHA) turns state into
    applied/pending; without it every entry is 'committed'.
    """
    gc = _gitcrud(request)
    try:
        entries, next_before = read_log(
            gc, limit=limit, before=before, applied_revision=applied_revision
        )
    except UnknownCursorError as exc:
        raise HTTPException(400, detail={"code": "unknown_cursor", "message": str(exc)}) from exc
    if group_by is not None:
        return GroupedLogResponse(
            groups=[
                LogGroup(key=g["key"], count=g["count"], latest=_entry_model(g["latest"]))
                for g in group_log(entries, group_by)
            ]
        )
    return LogResponse(entries=[_entry_model(e) for e in entries], next_before=next_before)
