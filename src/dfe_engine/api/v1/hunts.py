#  Project:      dfe-engine
#  File:         src/dfe_engine/api/v1/hunts.py
#  Purpose:      REST API for hunt engine status, listing, and ad-hoc execution
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Hunts router — engine status, hunt listing, and on-demand execution.

The HuntEngine runs as a background scheduler. This router provides:
- Engine lifecycle status
- Hunt directory listing
- On-demand hunt execution (returns 202 + task_id for polling)
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from dfe_engine.api.deps import CurrentUser, require_action
from dfe_engine.api.task_manager import TaskManager
from dfe_engine.auth.audit import audit_resource_change

router = APIRouter(prefix="/hunts", tags=["hunts"])


# ── Response models ─────────────────────────────────────────


class HuntEngineStatus(BaseModel):
    """Current state of the background hunt scheduler."""

    running: bool = Field(description="Whether the scheduler thread is alive")
    hunt_count: int = Field(default=0, description="Number of loaded hunts across all schedulers")
    scheduling_mode: str = Field(default="", description="Scheduling mode (cron, adaptive)")


class HuntSummary(BaseModel):
    """Summary of a configured hunt."""

    name: str
    customer: str
    cron: str = Field(description="Cron schedule expression")
    rules: list[str] = Field(default_factory=list)
    source_table: str = Field(default="")
    target_table: str = Field(default="")


class TriggerRequest(BaseModel):
    """Request to trigger an ad-hoc hunt execution."""

    customer: str = Field(description="Customer/org ID to run the hunt for")


class TriggerResponse(BaseModel):
    """Response from triggering an ad-hoc hunt."""

    task_id: str = Field(description="Task ID for polling via /tasks/{task_id}")
    hunt_name: str = Field(description="Name of the triggered hunt")


# ── Dependencies ────────────────────────────────────────────


def _get_hunt_engine(request: Request):
    return getattr(request.app.state, "hunt_engine", None)


def _get_task_manager(request: Request) -> TaskManager:
    return request.app.state.task_manager


# ── Endpoints ───────────────────────────────────────────────


@router.get("/status", response_model=HuntEngineStatus)
async def get_engine_status(
    request: Request,
    user: CurrentUser,
) -> HuntEngineStatus:
    """Get the current status of the hunt scheduler."""
    engine = _get_hunt_engine(request)
    if engine is None:
        return HuntEngineStatus(running=False)

    hunt_count = sum(len(cj.hunts) for cj in engine._cron_jobs)
    return HuntEngineStatus(
        running=engine.is_running,
        hunt_count=hunt_count,
        scheduling_mode=engine._settings.hunts.scheduling_mode,
    )


@router.get("", response_model=list[HuntSummary])
async def list_hunts(
    request: Request,
    user: CurrentUser,
) -> list[HuntSummary]:
    """List all configured hunts across all schedulers."""
    engine = _get_hunt_engine(request)
    if engine is None:
        return []

    summaries: list[HuntSummary] = []
    for cron_job in engine._cron_jobs:
        for hunt in cron_job.hunts:
            summaries.append(
                HuntSummary(
                    name=hunt.name,
                    customer=hunt.customer,
                    cron=hunt.cron,
                    rules=hunt.rules,
                    source_table=hunt.global_source_table_name,
                    target_table=hunt.global_target_table_name,
                )
            )
    return summaries


@router.post(
    "/{name}/run",
    response_model=TriggerResponse,
    status_code=202,
)
async def trigger_hunt(
    name: str,
    body: TriggerRequest,
    request: Request,
    user: CurrentUser,
    _auth: None = Depends(require_action("hunt:execute")),
) -> TriggerResponse:
    """Trigger an ad-hoc hunt execution.

    Returns 202 with a task_id that can be polled via ``GET /tasks/{task_id}``
    or streamed via ``GET /tasks/{task_id}/stream``.
    """
    engine = _get_hunt_engine(request)
    if engine is None:
        raise HTTPException(
            status_code=503,
            detail={"code": "not_configured", "message": "Hunt engine not running"},
        )

    # Find the hunt across all cron jobs
    target_hunt = None
    for cron_job in engine._cron_jobs:
        for hunt in cron_job.hunts:
            if hunt.name == name:
                target_hunt = hunt
                break
        if target_hunt:
            break

    if target_hunt is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Hunt '{name}' not found"},
        )

    manager = _get_task_manager(request)
    task_info = manager.submit(
        "hunt:execute",
        _execute_hunt,
        target_hunt,
        body.customer,
    )

    audit_resource_change(user.user_id, "hunt", name, "executed")
    return TriggerResponse(task_id=task_info.id, hunt_name=name)


async def _execute_hunt(hunt: Any, customer: str, *, task: Any) -> dict:
    """Run a single hunt for a customer. Called by TaskManager."""
    from datetime import UTC, datetime

    task.set_progress(10, f"Starting hunt '{hunt.name}' for customer '{customer}'")

    try:
        result = hunt.execute_hunt(
            customer=customer,
            scheduled_start_time=datetime.now(UTC),
        )
    except Exception as exc:
        raise RuntimeError(f"Hunt '{hunt.name}' failed: {exc}") from exc

    task.set_progress(90, "Hunt execution complete, collecting results")
    return result
