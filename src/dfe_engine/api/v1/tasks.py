#  Project:      dfe-engine
#  File:         src/dfe_engine/api/v1/tasks.py
#  Purpose:      REST API for background task status polling and SSE streaming
#  Language:     Python
#
#  License:      FSL-1.1-ALv2
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Tasks router — status polling and SSE streaming for background tasks.

Tasks are created by other routers (hunts, pipeline) via the TaskManager.
This router only provides read access + cancel.
"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, HTTPException, Query, Request
from sse_starlette.sse import EventSourceResponse

from dfe_engine.api.deps import CurrentUser
from dfe_engine.api.task_manager import TaskInfo, TaskManager

router = APIRouter(prefix="/tasks", tags=["tasks"])


def _get_task_manager(request: Request) -> TaskManager:
    return request.app.state.task_manager


@router.get("", response_model=list[TaskInfo])
async def list_tasks(
    request: Request,
    user: CurrentUser,
    kind: str | None = Query(None, description="Filter by task kind"),
) -> list[TaskInfo]:
    """List all tasks, optionally filtered by kind."""
    manager = _get_task_manager(request)
    return manager.list(kind=kind)


@router.get("/{task_id}", response_model=TaskInfo)
async def get_task(
    request: Request,
    task_id: str,
    user: CurrentUser,
) -> TaskInfo:
    """Get current status of a task."""
    manager = _get_task_manager(request)
    info = manager.get(task_id)
    if info is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Task '{task_id}' not found"},
        )
    return info


@router.post("/{task_id}/cancel", response_model=TaskInfo)
async def cancel_task(
    request: Request,
    task_id: str,
    user: CurrentUser,
) -> TaskInfo:
    """Cancel a running task."""
    manager = _get_task_manager(request)
    info = manager.get(task_id)
    if info is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Task '{task_id}' not found"},
        )
    manager.cancel(task_id)
    # Re-fetch after cancel request (status may not have changed yet)
    return manager.get(task_id)  # type: ignore[return-value]


@router.get("/{task_id}/stream")
async def stream_task(
    request: Request,
    task_id: str,
) -> EventSourceResponse:
    """SSE stream of task progress updates.

    Events:
    - ``progress``: ``{status, progress, message}``
    - ``complete``: ``{status, result, error}`` (terminal — stream ends)
    """
    manager = _get_task_manager(request)
    info = manager.get(task_id)
    if info is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Task '{task_id}' not found"},
        )

    async def _event_generator():
        while True:
            updated = await manager.wait_for_progress(task_id, timeout=15.0)
            if updated is None:
                return
            data = updated.model_dump(mode="json", exclude_none=True)
            if updated.status in ("completed", "failed", "cancelled"):
                yield {"event": "complete", "data": json.dumps(data)}
                return
            yield {"event": "progress", "data": json.dumps(data)}
            # Small delay to avoid tight loop if progress events fire rapidly
            await asyncio.sleep(0.1)

    return EventSourceResponse(_event_generator())
