#  Project:      dfe-engine
#  File:         api/v1/sampler.py
#  Purpose:      REST API for source sampling (recent/random/smart/anomaly)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Sampler router - pull sample data from a source (ClickHouse or Kafka).

One unified contract for fast and gated modes:

    POST /sources/{source}/sample   -> submit a sample for a registered source
    POST /sample                    -> submit an ad-hoc sample (explicit table/topic)
    GET  /samples/{task_id}         -> poll a sample's status/result
    GET  /samples                   -> list recent sample tasks

Every mode is submitted to the TaskManager. Fast modes (recent/random) are
blocked on for up to ``sampler.wait_seconds`` so they return ``completed`` with
the result inline (sync feel for the CLI / downstream APIs). Gated modes
(smart/anomaly) return ``pending`` immediately unless the caller passes ``wait``;
poll here or subscribe to ``GET /tasks/{task_id}/stream`` (SSE) for the UI.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from dfe_engine.api.deps import (
    CurrentUser,
    SourceReg,
    TenantClient,
    check_action,
    require_action,
)
from dfe_engine.api.pagination import PaginatedResponse, PaginationParams
from dfe_engine.api.task_manager import TaskInfo, TaskManager, TaskStatus
from dfe_engine.auth import AuthContext
from dfe_engine.auth.audit import audit_resource_change
from dfe_engine.auth.rbac_scopes import scopes_dict
from dfe_engine.sampling import GATED_MODES, Sampler, SampleRequest, SamplerError, SampleResult

router = APIRouter(tags=["sampler"])

_READ = Depends(require_action(scopes_dict["sampler_read"]))


class SampleSubmitResponse(BaseModel):
    """Envelope returned by a sample submit and by the poll endpoint."""

    task_id: str = Field(description="Task ID; poll via GET /samples/{task_id}")
    status: TaskStatus = Field(description="pending | running | completed | failed | cancelled")
    result: SampleResult | None = Field(default=None, description="Present once completed")
    error: str | None = Field(default=None, description="Present on failure")


def _sampler(request: Request) -> Sampler:
    sampler = getattr(request.app.state, "sampler", None)
    if sampler is None:  # pragma: no cover - always wired in lifespan
        raise HTTPException(
            503, detail={"code": "not_configured", "message": "Sampler not initialised"}
        )
    return sampler


def _task_manager(request: Request) -> TaskManager:
    return request.app.state.task_manager


def _to_response(info: TaskInfo) -> SampleSubmitResponse:
    result = None
    if info.status == TaskStatus.COMPLETED and isinstance(info.result, dict):
        result = SampleResult.model_validate(info.result)
    return SampleSubmitResponse(
        task_id=info.id, status=info.status, result=result, error=info.error
    )


async def _submit(
    req: SampleRequest,
    request: Request,
    ch: object,
    source_registry: object,
    user: AuthContext,
) -> SampleSubmitResponse:
    sampler = _sampler(request)
    manager = _task_manager(request)
    cfg = request.app.state.settings.sampler

    # A caller-supplied `table`/`filter` is raw SQL interpolated into the query, so
    # it requires `query:raw` - NOT the broad `sampler:read` that read-only viewers
    # hold. `query:raw` is held only by admin-class principals, whose fixed CH user
    # (dfe_admin) is targeted by NO row policy, so the interpolation runs
    # unrestricted (every org) - exactly why it must be walled off from viewers.
    # Sampling a REGISTERED source (no table/filter) stays `sampler:read` and runs
    # on the acting user's fixed client (row-filtered for org_analyst). Without
    # this a viewer reaches an arbitrary-SQL sink and reads every org
    # (F-SAMPLER-SQLI).
    if req.table is not None or req.filter is not None:
        check_action(request, user, scopes_dict["query_raw"])

    try:
        # Fail fast on a bad target (missing source, no table/topic, or a `table`
        # override that is not a bare db.table identifier) rather than burying it
        # in a background task the caller then has to poll for.
        sampler.resolve_or_raise(req, source_registry)
    except SamplerError as exc:
        raise HTTPException(
            400, detail={"code": "bad_sample_request", "message": str(exc)}
        ) from exc

    info = manager.submit("sampler:sample", sampler.run, req, ch, source_registry)
    audit_resource_change(
        user.user_id, "sampler", req.source or req.table or req.topic or "", "executed"
    )

    # Decide how long to block for inline completion.
    if req.wait is not None:
        wait = req.wait
    elif req.mode in GATED_MODES:
        wait = 0.0
    else:
        wait = cfg.wait_seconds

    if wait > 0:
        info = await manager.await_terminal(info.id, wait) or info
    return _to_response(info)


# ── endpoints ──────────────────────────────────────────────────


@router.post("/sources/{source}/sample", response_model=SampleSubmitResponse, dependencies=[_READ])
async def sample_source(
    source: str,
    body: SampleRequest,
    request: Request,
    user: CurrentUser,
    ch: TenantClient,
    source_registry: SourceReg,
) -> SampleSubmitResponse:
    """Sample a registered source. The path ``source`` wins over any in the body."""
    req = body.model_copy(update={"source": source})
    return await _submit(req, request, ch, source_registry, user)


@router.post("/sample", response_model=SampleSubmitResponse, dependencies=[_READ])
async def sample_adhoc(
    body: SampleRequest,
    request: Request,
    user: CurrentUser,
    ch: TenantClient,
    source_registry: SourceReg,
) -> SampleSubmitResponse:
    """Ad-hoc sample - supply an explicit ``table``/``topic`` (or a ``source``)."""
    return await _submit(body, request, ch, source_registry, user)


@router.get("/samples/{task_id}", response_model=SampleSubmitResponse, dependencies=[_READ])
async def get_sample(task_id: str, request: Request, user: CurrentUser) -> SampleSubmitResponse:
    """Poll a sample task's status and result."""
    info = _task_manager(request).get(task_id)
    # Guard the kind: _to_response validates info.result as a SampleResult, so a
    # wrong-kind task id (e.g. a completed hunt task) would 500 on validation.
    # Treat a non-sampler id the same as a missing one -> 404.
    if info is None or not info.kind.startswith("sampler:"):
        raise HTTPException(404, detail={"code": "not_found", "message": "sample task not found"})
    return _to_response(info)


@router.get("/samples", response_model=PaginatedResponse[TaskInfo], dependencies=[_READ])
async def list_samples(
    request: Request,
    user: CurrentUser,
    pagination: PaginationParams = Depends(),
) -> PaginatedResponse[TaskInfo]:
    """List recent sample tasks (most recent first), paginated."""
    items = _task_manager(request).list(kind="sampler:sample")
    return PaginatedResponse.from_list(items, pagination.page, pagination.per_page)
