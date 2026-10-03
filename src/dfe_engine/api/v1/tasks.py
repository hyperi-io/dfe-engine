#  Project:      dfe-engine
#  File:         src/dfe_engine/api/v1/tasks.py
#  Purpose:      REST API for background task status polling and SSE streaming
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Tasks router -- status polling and SSE streaming for background tasks.

Tasks are created by other routers (hunts, pipeline) via the TaskManager.
This router only provides read access + cancel.

A caller without a platform grant holding the route's action is held to its own
orgs, as ``GET /samples`` holds it: it sees only the tasks held to them, and gets
404 for any other task, as for one that does not exist.
"""

import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sse_starlette.sse import EventSourceResponse

from dfe_engine.api.deps import CurrentUser, held_tenant_ids, require_action
from dfe_engine.api.task_manager import TaskInfo, TaskManager
from dfe_engine.auth.models import AuthContext
from dfe_engine.auth.rbac_scopes import scopes_dict

router = APIRouter(prefix="/tasks", tags=["tasks"])


def _get_task_manager(request: Request) -> TaskManager:
    return request.app.state.task_manager


def _visible_task(request: Request, user: AuthContext, task_id: str, action: str) -> TaskInfo:
    """The task, if the caller may see it under ``action``.

    Raises:
        HTTPException: 404 when the task does not exist or is not held to the caller's orgs.
    """
    reader_orgs = held_tenant_ids(request, user, action)
    info = _get_task_manager(request).get(task_id, reader_orgs=reader_orgs)
    if info is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Task '{task_id}' not found"},
        )
    return info


@router.get(
    "",
    response_model=list[TaskInfo],
    dependencies=[Depends(require_action(scopes_dict["task_read"]))],
)
async def list_tasks(
    request: Request,
    user: CurrentUser,
    kind: str | None = Query(None, description="Filter by task kind"),
) -> list[TaskInfo]:
    """List the tasks the caller may see, optionally filtered by kind."""
    reader_orgs = held_tenant_ids(request, user, scopes_dict["task_read"])
    return _get_task_manager(request).list(kind=kind, reader_orgs=reader_orgs)


@router.get(
    "/{task_id}",
    response_model=TaskInfo,
    dependencies=[Depends(require_action(scopes_dict["task_read"]))],
)
async def get_task(
    request: Request,
    task_id: str,
    user: CurrentUser,
) -> TaskInfo:
    """Get current status of a task."""
    return _visible_task(request, user, task_id, scopes_dict["task_read"])


@router.post(
    "/{task_id}/cancel",
    response_model=TaskInfo,
    dependencies=[Depends(require_action(scopes_dict["task_write"]))],
)
async def cancel_task(
    request: Request,
    task_id: str,
    user: CurrentUser,
) -> TaskInfo:
    """Cancel a running task."""
    info = _visible_task(request, user, task_id, scopes_dict["task_write"])
    manager = _get_task_manager(request)
    manager.cancel(task_id)
    # Re-fetch after cancel request (status may not have changed yet); fall back
    # to the pre-cancel info if the task vanished between cancel and re-fetch.
    return manager.get(task_id) or info


@router.get(
    "/{task_id}/stream",
    response_class=Response,
    include_in_schema=False,
    dependencies=[Depends(require_action(scopes_dict["task_read"]))],
)
async def stream_task(
    request: Request,
    task_id: str,
    user: CurrentUser,
):
    """SSE stream of task progress updates.

    Events:
    - ``progress``: ``{status, progress, message}``
    - ``complete``: ``{status, result, error}`` (terminal -- stream ends)
    """
    # A task's hold is fixed at submit, so the check here covers every event after it.
    _visible_task(request, user, task_id, scopes_dict["task_read"])
    manager = _get_task_manager(request)

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
