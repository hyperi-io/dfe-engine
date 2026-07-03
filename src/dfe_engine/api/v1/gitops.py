#  Project:      dfe-engine
#  File:         api/v1/gitops.py
#  Purpose:      Governed Ops - engine-wide gitops settings (auto-merge)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Auto-merge status + toggle (the dfe-ui banner contract).

GET /api/v1/gitops/auto-merge -> {stored, effective, allowed, reason} (governance:read)
PUT /api/v1/gitops/auto-merge -> toggle; refuses to enable when the deployment
gate (dev posture OR DFE_GITOPS_MODE=solo) does not permit (governance:write).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from scalo.logger import logger

from dfe_engine.api.deps import CurrentUser, require_action
from dfe_engine.auth.audit import audit_resource_change
from dfe_engine.gitcrud import GitCrud
from dfe_engine.gitcrud.auto_merge import AutoMergeState, resolve_state, set_stored

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


def _state(request: Request) -> AutoMergeState:
    settings = request.app.state.settings
    return resolve_state(_gitcrud(request), environment=settings.env, mode=settings.gitops.mode)


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
    return _status(_state(request))


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
    state = _state(request)
    if body.enabled and not state.allowed:
        raise HTTPException(403, detail={"code": "auto_merge_forbidden", "message": state.reason})
    if body.enabled:
        logger.warning("AUTO-MERGE enabled via API", actor=user.user_id)
    set_stored(gc, body.enabled, user.user_id)
    audit_resource_change(
        user.user_id, "gitops", "auto-merge", "updated", {"enabled": body.enabled}
    )
    return _status(_state(request))
