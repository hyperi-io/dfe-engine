#  Project:      dfe-engine
#  File:         api/v1/synthetic_data.py
#  Purpose:      REST API for synthetic reference-data generation
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Synthetic data router - generate realistic synthetic data from reference packs.

    GET    /synthetic-data/packs               -> list generatable schema packs
    POST   /synthetic-data/generate            -> bounded inline batch (demo/test data)
    POST   /synthetic-data/lookalike           -> batch shaped like a supplied sample
    POST   /synthetic-data/stream              -> start a stream posting to a receiver
    GET    /synthetic-data/streams             -> list streams
    GET    /synthetic-data/streams/{operation} -> poll a stream
    POST   /synthetic-data/streams/{operation}/cancel -> cancel a running stream

Reads need ``synthetic-data:read``; generating or streaming needs ``synthetic-data:run``
(generation injects data into the pipeline, so it is a write-grade action).
A stream is an operation, polled at the ``Location`` its start answers.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from scalo.api import ACCEPTED_RESPONSES, ListParams, Page, ProblemError, accept, paginate

from dfe_engine.api.deps import CurrentUser, require_action
from dfe_engine.api.task_manager import (
    TaskInfo,
    TaskManager,
    TaskOperation,
    TaskStatus,
    task_operation,
)
from dfe_engine.auth.audit import audit_resource_change
from dfe_engine.auth.rbac_scopes import scopes_dict
from dfe_engine.synthetic_data.models import (
    GenerateRequest,
    GenerateResult,
    LookalikeRequest,
    PackInfo,
    StreamRequest,
    SyntheticDataError,
)
from dfe_engine.synthetic_data.service import SyntheticDataService

router = APIRouter(tags=["synthetic-data"])

_READ = Depends(require_action(scopes_dict["synthetic_data_read"]))
_RUN = Depends(require_action(scopes_dict["synthetic_data_run"]))

_TASK_KIND = "synthetic-data:stream"


def _service(request: Request) -> SyntheticDataService:
    service = getattr(request.app.state, "synthetic_data", None)
    if service is None:  # pragma: no cover - always wired in lifespan
        raise ProblemError(503, "Synthetic data not initialised", code="not_configured")
    return service


def _task_manager(request: Request) -> TaskManager:
    return request.app.state.task_manager


def _bad_request(exc: SyntheticDataError) -> ProblemError:
    return ProblemError(400, str(exc), code="bad_synthetic_data_request")


# -- endpoints --------------------------------------------------


@router.get("/synthetic-data/packs", response_model=Page[PackInfo], dependencies=[_READ])
def list_packs(query: ListParams, request: Request, user: CurrentUser) -> Page[PackInfo]:
    """List schema packs the generator can drive."""
    try:
        return paginate(_service(request).list_packs(), query)
    except SyntheticDataError as exc:
        raise _bad_request(exc) from exc


@router.post("/synthetic-data/generate", response_model=GenerateResult, dependencies=[_RUN])
def generate(body: GenerateRequest, request: Request, user: CurrentUser) -> GenerateResult:
    """Generate a bounded batch of synthetic events inline.

    Sync handler by design: a ceiling-sized batch is pure CPU, so FastAPI's
    threadpool keeps the event loop responsive while it renders.
    """
    try:
        result = _service(request).generate(body)
    except SyntheticDataError as exc:
        raise _bad_request(exc) from exc
    audit_resource_change(user.user_id, "synthetic-data", body.schema_ref, "executed")
    return result


@router.post("/synthetic-data/lookalike", response_model=GenerateResult, dependencies=[_RUN])
def lookalike(body: LookalikeRequest, request: Request, user: CurrentUser) -> GenerateResult:
    """Generate a lookalike batch from a supplied sample (identities scrubbed)."""
    try:
        result = _service(request).generate_lookalike(body)
    except SyntheticDataError as exc:
        raise _bad_request(exc) from exc
    audit_resource_change(user.user_id, "synthetic-data", "sample", "executed")
    return result


@router.post(
    "/synthetic-data/stream",
    response_model=TaskOperation,
    status_code=202,
    responses=ACCEPTED_RESPONSES,
    dependencies=[_RUN],
)
async def start_stream(
    body: StreamRequest, request: Request, response: Response, user: CurrentUser
) -> TaskOperation:
    """Start a stream task that posts generated events to an ingest URL."""
    service = _service(request)
    try:
        # Fail fast on a bad request rather than burying it in a task.
        service.validate_stream(body)
    except SyntheticDataError as exc:
        raise _bad_request(exc) from exc

    manager = _task_manager(request)
    active = [
        t
        for t in manager.list(kind=_TASK_KIND)
        if t.status in (TaskStatus.PENDING, TaskStatus.RUNNING)
    ]
    cap = service.settings.max_concurrent_streams
    if len(active) >= cap:
        raise ProblemError(
            409,
            f"{len(active)} streams already running (cap {cap}) - "
            "cancel one or raise synthetic_data.max_concurrent_streams",
            code="too_many_streams",
        )
    info = manager.submit(_TASK_KIND, service.run_stream, body, reportable=(SyntheticDataError,))
    audit_resource_change(
        user.user_id,
        "synthetic-data",
        body.schema_ref,
        "executed",
        details={"receiver_url": body.receiver_url},
    )

    if body.wait:
        info = await manager.await_terminal(info.id, body.wait) or info
    location = f"/api/v1/synthetic-data/streams/{info.id}"
    return accept(response, task_operation(info), location=location)


@router.get("/synthetic-data/streams", response_model=Page[TaskOperation], dependencies=[_READ])
async def list_streams(
    query: ListParams, request: Request, user: CurrentUser
) -> Page[TaskOperation]:
    """List stream operations (most recent first)."""
    streams = _task_manager(request).list(kind=_TASK_KIND)
    return paginate([task_operation(info) for info in streams], query)


def _stream(request: Request, operation: str) -> TaskInfo:
    """The stream's task.

    Raises:
        ProblemError: 404 when no stream has that id.
    """
    info = _task_manager(request).get(operation)
    if info is None or info.kind != _TASK_KIND:
        raise ProblemError(404, "stream not found", code="not_found")
    return info


@router.get(
    "/synthetic-data/streams/{operation}", response_model=TaskOperation, dependencies=[_READ]
)
async def get_stream(operation: str, request: Request, user: CurrentUser) -> TaskOperation:
    """Poll a stream, under the same action that lists it."""
    return task_operation(_stream(request, operation))


@router.post(
    "/synthetic-data/streams/{operation}/cancel", response_model=TaskOperation, dependencies=[_RUN]
)
async def cancel_stream(operation: str, request: Request, user: CurrentUser) -> TaskOperation:
    """Cancel a stream, under the same action that starts it."""
    info = _stream(request, operation)
    manager = _task_manager(request)
    manager.cancel(info.id)
    audit_resource_change(user.user_id, "synthetic-data", info.id, "cancelled")
    # The status may not have moved yet, and the task may be evicted in between.
    return task_operation(manager.get(info.id) or info)
