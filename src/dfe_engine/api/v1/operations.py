#  Project:      dfe-engine
#  File:         src/dfe_engine/api/v1/operations.py
#  Purpose:      Long-running operations: list, poll, cancel and stream background tasks
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Operations router: every 202 the engine answers points here to poll.

Other routers start the work through the TaskManager and answer
:func:`~dfe_engine.api.task_manager.accept_task`.

A caller without a platform grant holding the route's action is held to its own
orgs, as ``GET /samples`` holds it: it sees only the operations held to them, and
gets 404 for any other, as for one that does not exist.
"""

import asyncio
import json

from fastapi import APIRouter, Depends, Query, Request, Response
from scalo.api import ListParams, Page, ProblemError, paginate
from sse_starlette.sse import EventSourceResponse

from dfe_engine.api.deps import CurrentUser, held_tenant_ids, require_own_org_action
from dfe_engine.api.task_manager import TaskInfo, TaskManager, TaskOperation, task_operation
from dfe_engine.auth.models import AuthContext
from dfe_engine.auth.rbac_scopes import scopes_dict

router = APIRouter(prefix="/operations", tags=["operations"])


def _get_task_manager(request: Request) -> TaskManager:
    return request.app.state.task_manager


def _visible_task(request: Request, user: AuthContext, operation: str, action: str) -> TaskInfo:
    """The task, if the caller may see it under ``action``.

    Raises:
        ProblemError: 404 when the task does not exist or is not held to the caller's orgs.
    """
    reader_orgs = held_tenant_ids(request, user, action)
    info = _get_task_manager(request).get(operation, reader_orgs=reader_orgs)
    if info is None:
        raise ProblemError(404, f"Operation '{operation}' not found", code="not_found")
    return info


@router.get(
    "",
    response_model=Page[TaskOperation],
    dependencies=[Depends(require_own_org_action(scopes_dict["task_read"]))],
)
async def list_operations(
    query: ListParams,
    request: Request,
    user: CurrentUser,
    kind: str | None = Query(None, description="Filter by task kind"),
) -> Page[TaskOperation]:
    """List the operations the caller may see, optionally filtered by kind."""
    reader_orgs = held_tenant_ids(request, user, scopes_dict["task_read"])
    tasks = _get_task_manager(request).list(kind=kind, reader_orgs=reader_orgs)
    return paginate([task_operation(info) for info in tasks], query, bound={"kind": kind})


@router.get(
    "/{operation}",
    response_model=TaskOperation,
    dependencies=[Depends(require_own_org_action(scopes_dict["task_read"]))],
)
async def get_operation(request: Request, operation: str, user: CurrentUser) -> TaskOperation:
    """Get an operation: ``done`` with its ``response`` or ``error``, else its progress."""
    return task_operation(_visible_task(request, user, operation, scopes_dict["task_read"]))


@router.post(
    "/{operation}/cancel",
    response_model=TaskOperation,
    dependencies=[Depends(require_own_org_action(scopes_dict["task_write"]))],
)
async def cancel_operation(request: Request, operation: str, user: CurrentUser) -> TaskOperation:
    """Cancel a running operation."""
    info = _visible_task(request, user, operation, scopes_dict["task_write"])
    manager = _get_task_manager(request)
    manager.cancel(operation)
    # The status may not have moved yet, and the task may be evicted in between.
    return task_operation(manager.get(operation) or info)


@router.get(
    "/{operation}/stream",
    response_class=Response,
    include_in_schema=False,
    dependencies=[Depends(require_own_org_action(scopes_dict["task_read"]))],
)
async def stream_operation(request: Request, operation: str, user: CurrentUser):
    """SSE stream of an operation's progress.

    Events:
    - ``progress``: ``{status, progress, message}``
    - ``complete``: ``{status, result, error}`` (terminal -- stream ends)
    """
    # A task's hold is fixed at submit, so the check here covers every event after it.
    _visible_task(request, user, operation, scopes_dict["task_read"])
    manager = _get_task_manager(request)

    async def _event_generator():
        while True:
            updated = await manager.wait_for_progress(operation, timeout=15.0)
            if updated is None:
                return
            data = updated.model_dump(mode="json", exclude_none=True)
            if updated.status.is_terminal:
                yield {"event": "complete", "data": json.dumps(data)}
                return
            yield {"event": "progress", "data": json.dumps(data)}
            # Small delay to avoid tight loop if progress events fire rapidly
            await asyncio.sleep(0.1)

    return EventSourceResponse(_event_generator())
