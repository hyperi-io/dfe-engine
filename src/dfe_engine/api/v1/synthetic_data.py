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
    POST   /synthetic-data/stream              -> start a stream task posting to a receiver
    GET    /synthetic-data/streams             -> list stream tasks
    GET    /synthetic-data/streams/{task_id}   -> poll a stream task
    DELETE /synthetic-data/streams/{task_id}   -> cancel a running stream

Reads need ``synthetic-data:read``; generating or streaming needs ``synthetic-data:run``
(generation injects data into the pipeline, so it is a write-grade action).
Streams are TaskManager tasks: poll here or subscribe to
``GET /tasks/{task_id}/stream`` (SSE) for the UI.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from dfe_engine.api.deps import CurrentUser, require_action
from dfe_engine.api.task_manager import TaskInfo, TaskManager, TaskStatus
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


class StreamSubmitResponse(BaseModel):
    """Envelope returned by a stream submit and by the poll endpoint."""

    task_id: str = Field(description="Task ID; poll via GET /synthetic-data/streams/{task_id}")
    status: TaskStatus = Field(description="pending | running | completed | failed | cancelled")
    result: dict | None = Field(default=None, description="Stream summary once terminal")
    error: str | None = Field(default=None, description="Present on failure")


class CancelResponse(BaseModel):
    """Result of a cancel request."""

    task_id: str
    cancelled: bool = Field(description="True when cancellation was requested")


def _service(request: Request) -> SyntheticDataService:
    service = getattr(request.app.state, "synthetic_data", None)
    if service is None:  # pragma: no cover - always wired in lifespan
        raise HTTPException(
            503, detail={"code": "not_configured", "message": "Synthetic data not initialised"}
        )
    return service


def _task_manager(request: Request) -> TaskManager:
    return request.app.state.task_manager


def _to_response(info: TaskInfo) -> StreamSubmitResponse:
    result = info.result if isinstance(info.result, dict) else None
    return StreamSubmitResponse(
        task_id=info.id, status=info.status, result=result, error=info.error
    )


def _bad_request(exc: SyntheticDataError) -> HTTPException:
    return HTTPException(400, detail={"code": "bad_synthetic_data_request", "message": str(exc)})


# ── endpoints ──────────────────────────────────────────────────


@router.get("/synthetic-data/packs", response_model=list[PackInfo], dependencies=[_READ])
async def list_packs(request: Request, user: CurrentUser) -> list[PackInfo]:
    """List schema packs the generator can drive."""
    try:
        return _service(request).list_packs()
    except SyntheticDataError as exc:
        raise _bad_request(exc) from exc


@router.post("/synthetic-data/generate", response_model=GenerateResult, dependencies=[_RUN])
async def generate(body: GenerateRequest, request: Request, user: CurrentUser) -> GenerateResult:
    """Generate a bounded batch of synthetic events inline."""
    try:
        result = _service(request).generate(body)
    except SyntheticDataError as exc:
        raise _bad_request(exc) from exc
    audit_resource_change(user.user_id, "synthetic-data", body.schema_ref, "executed")
    return result


@router.post("/synthetic-data/lookalike", response_model=GenerateResult, dependencies=[_RUN])
async def lookalike(body: LookalikeRequest, request: Request, user: CurrentUser) -> GenerateResult:
    """Generate a lookalike batch from a supplied sample (identities scrubbed)."""
    try:
        result = _service(request).generate_lookalike(body)
    except SyntheticDataError as exc:
        raise _bad_request(exc) from exc
    audit_resource_change(user.user_id, "synthetic-data", "sample", "executed")
    return result


@router.post("/synthetic-data/stream", response_model=StreamSubmitResponse, dependencies=[_RUN])
async def start_stream(
    body: StreamRequest, request: Request, user: CurrentUser
) -> StreamSubmitResponse:
    """Start a stream task that posts generated events to an ingest URL."""
    service = _service(request)
    try:
        # Fail fast on a bad request rather than burying it in a task.
        service.validate_stream(body)
    except SyntheticDataError as exc:
        raise _bad_request(exc) from exc

    manager = _task_manager(request)
    info = manager.submit(_TASK_KIND, service.run_stream, body)
    audit_resource_change(user.user_id, "synthetic-data", body.schema_ref, "executed")

    if body.wait:
        info = await manager.await_terminal(info.id, body.wait) or info
    return _to_response(info)


@router.get("/synthetic-data/streams", response_model=list[TaskInfo], dependencies=[_READ])
async def list_streams(request: Request, user: CurrentUser) -> list[TaskInfo]:
    """List stream tasks (most recent first)."""
    return _task_manager(request).list(kind=_TASK_KIND)


@router.get(
    "/synthetic-data/streams/{task_id}", response_model=StreamSubmitResponse, dependencies=[_READ]
)
async def get_stream(task_id: str, request: Request, user: CurrentUser) -> StreamSubmitResponse:
    """Poll a stream task's status and summary."""
    info = _task_manager(request).get(task_id)
    if info is None or info.kind != _TASK_KIND:
        raise HTTPException(404, detail={"code": "not_found", "message": "stream task not found"})
    return _to_response(info)


@router.delete(
    "/synthetic-data/streams/{task_id}", response_model=CancelResponse, dependencies=[_RUN]
)
async def cancel_stream(task_id: str, request: Request, user: CurrentUser) -> CancelResponse:
    """Cancel a running stream task."""
    manager = _task_manager(request)
    info = manager.get(task_id)
    if info is None or info.kind != _TASK_KIND:
        raise HTTPException(404, detail={"code": "not_found", "message": "stream task not found"})
    cancelled = manager.cancel(task_id)
    audit_resource_change(user.user_id, "synthetic-data", task_id, "cancelled")
    return CancelResponse(task_id=task_id, cancelled=cancelled)
