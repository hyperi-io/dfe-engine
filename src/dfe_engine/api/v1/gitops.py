#  Project:      dfe-engine
#  File:         api/v1/gitops.py
#  Purpose:      Governed Ops - engine-wide gitops settings (auto-merge)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Auto-merge status + toggle (the dfe-ui banner contract), and the audit log.

GET /api/v1/gitops/auto-merge -> {stored, effective, allowed, reason} (governance:read)
PUT /api/v1/gitops/auto-merge -> toggle; refuses to enable when the deployment
gate (dev posture OR DFE_GITOPS_MODE=solo) does not permit (governance:write).
GET /api/v1/gitops/log -> flat {entries, next_before} or, with ?group_by=,
{groups} - every gitcrud commit, newest-first (governance:read).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from scalo.logger import logger

from dfe_engine.api.deps import CurrentUser, require_action
from dfe_engine.auth.audit import audit_resource_change
from dfe_engine.gitcrud import GitCrud
from dfe_engine.gitcrud.auto_merge import AutoMergeState, resolve_state, set_stored
from dfe_engine.gitcrud.log import LogEntry, UnknownCursorError, group_log, read_log

router = APIRouter(prefix="/gitops", tags=["Governed Ops: Gitops"])


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
    dependencies=[Depends(require_action("governance:read"))],
)
async def get_auto_merge(user: CurrentUser, request: Request) -> AutoMergeStatus:
    """Auto-merge status for the UI banner: stored flag, gate verdict, net effect."""
    # warn=False: the UI polls this - a stranded flag must not WARN per poll.
    return _status(_state(request, warn=False))


@router.put(
    "/auto-merge",
    response_model=AutoMergeStatus,
    dependencies=[Depends(require_action("governance:write"))],
)
async def put_auto_merge(
    body: AutoMergeRequest, user: CurrentUser, request: Request
) -> AutoMergeStatus:
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
    dependencies=[Depends(require_action("governance:read"))],
)
async def get_log(
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
