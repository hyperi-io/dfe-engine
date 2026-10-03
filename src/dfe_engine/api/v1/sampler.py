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

A caller without a platform grant holding ``sampler:read`` is held to its own
orgs: every read binds ``_org_id`` to its ``org_ids``, a target that cannot be
held to them is refused with a 403, and the poll and list routes show it only
the tasks held to its orgs.
"""

import functools
from collections import OrderedDict

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from scalo.concurrency import run_blocking

from dfe_engine.api.deps import (
    ClickHouseClient,
    CurrentUser,
    SourceReg,
    is_action_allowed,
    require_action,
)
from dfe_engine.api.task_manager import TaskInfo, TaskManager, TaskStatus
from dfe_engine.auth.audit import audit_resource_change
from dfe_engine.auth.models import AuthContext, ScopedGrant, platform_grants
from dfe_engine.auth.rbac_scopes import scopes_dict
from dfe_engine.sampling import (
    GATED_MODES,
    Sampler,
    SampleRequest,
    SamplerError,
    SampleResult,
    SampleScopeError,
)

router = APIRouter(tags=["sampler"])

_READ = Depends(require_action(scopes_dict["sampler_read"]))

_TERMINAL = {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}

# Above the task manager's 1000 kept tasks; an id that falls out is hidden from a held caller.
_TASK_SCOPE_CAPACITY = 4096


class _TaskScopes:
    """The orgs each sample task was held to, keyed by task id, oldest dropped first."""

    def __init__(self, capacity: int) -> None:
        self._held: OrderedDict[str, frozenset[str] | None] = OrderedDict()
        self._capacity = capacity

    def record(self, task_id: str, org_ids: list[str] | None) -> None:
        """Remember the orgs ``task_id`` was held to; None means it read every org."""
        self._held[task_id] = None if org_ids is None else frozenset(org_ids)
        while len(self._held) > self._capacity:
            self._held.popitem(last=False)

    def visible(self, task_id: str, org_ids: list[str] | None) -> bool:
        """Whether a caller held to ``org_ids`` may see ``task_id``; None sees every task."""
        if org_ids is None:
            return True
        held = self._held.get(task_id)
        return held is not None and held <= frozenset(org_ids)


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


def _task_scopes(request: Request) -> _TaskScopes:
    scopes = getattr(request.app.state, "sample_task_scopes", None)
    if scopes is None:
        scopes = _TaskScopes(_TASK_SCOPE_CAPACITY)
        request.app.state.sample_task_scopes = scopes
    return scopes


def sample_org_scope(request: Request, user: AuthContext) -> list[str] | None:
    """The orgs a caller's samples are held to, or None when it may read every org.

    A caller reads every org when a grant ``platform_grants`` keeps holds
    ``sampler:read``. Any other caller is held to its own ``org_ids``, and an empty
    list is refused by the sampler rather than read.

    Args:
        request: The request, for the role config.
        user: The caller's auth context.

    Returns:
        The caller's org ids, sorted, or None for a platform caller.
    """
    # Bare roles are system-scope grants, as authorize() reads a context without grants.
    grants = user.grants or [ScopedGrant(role=name) for name in user.roles]
    platform = platform_grants(grants)
    if platform:
        reader = user.model_copy(
            update={"roles": [grant.role for grant in platform], "grants": platform}
        )
        if is_action_allowed(request, reader, scopes_dict["sampler_read"]):
            return None
    return sorted(set(user.org_ids))


async def check_sample_scope(
    sampler: Sampler,
    req: SampleRequest,
    ch: object,
    source_registry: object,
    org_ids: list[str] | None,
) -> None:
    """Refuse with a 403 a sample that cannot be held to ``org_ids``.

    Raises:
        HTTPException: 403 naming why the sample cannot be held to the caller's
            orgs; 400 when the target cannot be described.
    """
    if org_ids is None:
        return
    try:
        await run_blocking(
            functools.partial(sampler.check_org_scope, req, ch, source_registry, org_ids)
        )
    except SampleScopeError as exc:
        raise HTTPException(403, detail={"code": "forbidden", "message": str(exc)}) from exc
    except SamplerError as exc:
        raise HTTPException(
            400, detail={"code": "bad_sample_request", "message": str(exc)}
        ) from exc


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

    try:
        # Fail fast on a bad target (missing source, no table/topic) rather than
        # burying it in a background task the caller then has to poll for.
        sampler.resolve_or_raise(req, source_registry)
    except SamplerError as exc:
        raise HTTPException(
            400, detail={"code": "bad_sample_request", "message": str(exc)}
        ) from exc
    org_ids = sample_org_scope(request, user)
    await check_sample_scope(sampler, req, ch, source_registry, org_ids)

    info = manager.submit("sampler:sample", sampler.run, req, ch, source_registry, org_ids=org_ids)
    _task_scopes(request).record(info.id, org_ids)
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


# -- endpoints --------------------------------------------------


@router.post("/sources/{source}/sample", response_model=SampleSubmitResponse, dependencies=[_READ])
async def sample_source(
    source: str,
    body: SampleRequest,
    request: Request,
    user: CurrentUser,
    ch: ClickHouseClient,
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
    ch: ClickHouseClient,
    source_registry: SourceReg,
) -> SampleSubmitResponse:
    """Ad-hoc sample - supply an explicit ``table``/``topic`` (or a ``source``)."""
    return await _submit(body, request, ch, source_registry, user)


@router.get("/samples/{task_id}", response_model=SampleSubmitResponse, dependencies=[_READ])
async def get_sample(task_id: str, request: Request, user: CurrentUser) -> SampleSubmitResponse:
    """Poll a sample task's status and result.

    A caller held to its orgs gets 404 for a task not held to them, as for one that
    does not exist.
    """
    org_ids = sample_org_scope(request, user)
    info = _task_manager(request).get(task_id)
    if info is None or not _task_scopes(request).visible(task_id, org_ids):
        raise HTTPException(404, detail={"code": "not_found", "message": "sample task not found"})
    return _to_response(info)


@router.get("/samples", response_model=list[TaskInfo], dependencies=[_READ])
async def list_samples(request: Request, user: CurrentUser) -> list[TaskInfo]:
    """List recent sample tasks (most recent first); a caller held to its orgs sees only theirs."""
    org_ids = sample_org_scope(request, user)
    scopes = _task_scopes(request)
    tasks = _task_manager(request).list(kind="sampler:sample")
    return [info for info in tasks if scopes.visible(info.id, org_ids)]
