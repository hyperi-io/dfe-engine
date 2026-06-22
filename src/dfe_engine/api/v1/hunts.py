#  Project:      dfe-engine
#  File:         src/dfe_engine/api/v1/hunts.py
#  Purpose:      REST API for hunt engine status, listing, and ad-hoc execution
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Hunts router — engine status, hunt config CRUD, and on-demand execution.

The HuntEngine runs as a background scheduler. This router provides:
- Engine lifecycle status
- Hunt configuration CRUD (YAML under ``hunts.hunt_dir``)
- Paginated hunt list with search
- On-demand hunt execution (returns 202 + task_id for polling)
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator

from dfe_engine.api.deps import CurrentUser, HuntConfigReg, require_action
from dfe_engine.api.pagination import PaginatedResponse, PaginationParams, apply_search, apply_sort
from dfe_engine.api.task_manager import TaskManager
from dfe_engine.auth.audit import audit_resource_change
from dfe_engine.auth.rbac_scopes import scopes_dict
from dfe_engine.hunts.hunt_config_registry import HuntConfigNotFoundError

_HUNT_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")

router = APIRouter(prefix="/hunts", tags=["hunts"])


# ── Response models ─────────────────────────────────────────


class HuntEngineStatus(BaseModel):
    """Current state of the background hunt scheduler."""

    running: bool = Field(description="Whether the scheduler thread is alive")
    hunt_count: int = Field(default=0, description="Number of loaded hunts across all schedulers")
    scheduling_mode: str = Field(default="", description="Scheduling mode (cron, adaptive)")


class HuntSummary(BaseModel):
    """Summary of a configured hunt."""

    hunt_id: str = Field(description="Stable hunt identifier (YAML filename stem)")
    name: str
    customer: str = Field(default="", description="First customer in config (legacy summary field)")
    customers: list[str] = Field(default_factory=list)
    cron: str | list[str] = Field(default="", description="Cron schedule expression(s)")
    rules: list[str] = Field(default_factory=list)
    source_table: str = Field(default="")
    target_table: str = Field(default="")


class HuntDetailResponse(BaseModel):
    """Full hunt configuration document."""

    hunt_id: str
    config: dict[str, Any]


class HuntRuleEntry(BaseModel):
    rule_name: str
    target_table_name: str | None = None
    source: str | None = None
    initial_checkpoint_lookback_minutes: int | None = None


class HuntWriteRequest(BaseModel):
    """Hunt scheduler YAML payload (without ``hunt_id``)."""

    name: str = Field(description="Display name used by the hunt engine")
    cron: str | list[str] = Field(description="Cron expression or list of expressions")
    log_buffer: int = Field(default=60, ge=1)
    global_target_table_name: str
    global_source_table_name: str | None = None
    customers: list[str] = Field(min_length=1)
    rules: list[HuntRuleEntry] = Field(min_length=1)
    customer_filters: dict[str, Any] | None = None
    checkpoint_timestamp_field: str | None = None
    scheduling_mode: str | None = None
    min_interval_seconds: int | None = None
    explain_queries: bool | None = None

    def to_config_dict(self) -> dict[str, Any]:
        data = self.model_dump(exclude_none=True)
        data["rules"] = [r.model_dump(exclude_none=True) for r in self.rules]
        return data


class HuntCreateRequest(HuntWriteRequest):
    hunt_id: str = Field(description="Stable id / YAML filename stem: [a-z][a-z0-9_]*")

    @field_validator("hunt_id")
    @classmethod
    def _validate_hunt_id(cls, v: str) -> str:
        if not _HUNT_ID_PATTERN.match(v):
            raise ValueError(f"Hunt id '{v}' must match [a-z][a-z0-9_]*")
        return v


class TriggerRequest(BaseModel):
    """Request to trigger an ad-hoc hunt execution."""

    customer: str = Field(description="Customer/org ID to run the hunt for")


class TriggerResponse(BaseModel):
    """Response from triggering an ad-hoc hunt."""

    task_id: str = Field(description="Task ID for polling via /tasks/{task_id}")
    hunt_name: str = Field(description="Display name of the triggered hunt")


# ── Dependencies ────────────────────────────────────────────


def _get_hunt_engine(request: Request):
    return getattr(request.app.state, "hunt_engine", None)


def _get_task_manager(request: Request) -> TaskManager:
    return request.app.state.task_manager


def _hunt_display_name_matches(hunt: Any, hunt_id: str) -> bool:
    """Match engine ``Hunt`` instance to registry ``hunt_id`` or display name."""
    if hunt.name == hunt_id:
        return True
    return hunt.name.replace(" ", "_") == hunt_id


def _hunt_row_to_summary(row: dict[str, Any]) -> HuntSummary:
    customers = row.get("customers") or []
    return HuntSummary(
        hunt_id=row["hunt_id"],
        name=row["name"],
        customer=customers[0] if customers else "",
        customers=customers,
        cron=row.get("cron", ""),
        rules=row.get("rules", []),
        source_table=row.get("source_table", ""),
        target_table=row.get("target_table", ""),
    )


def _find_engine_hunt(engine: Any, hunt_id: str, registry: HuntConfigReg | None):
    display_name = hunt_id
    if registry is not None:
        try:
            display_name = registry.get(hunt_id).get("name", hunt_id)
        except HuntConfigNotFoundError:
            pass

    for cron_job in engine._cron_jobs:
        for hunt in cron_job.hunts:
            if hunt.name == display_name or _hunt_display_name_matches(hunt, hunt_id):
                return hunt
    return None


# ── Endpoints ───────────────────────────────────────────────


@router.get(
    "/status",
    response_model=HuntEngineStatus,
    dependencies=[Depends(require_action(scopes_dict["hunt_read"]))],
)
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


@router.get(
    "",
    response_model=PaginatedResponse[HuntSummary],
    dependencies=[Depends(require_action(scopes_dict["hunt_read"]))],
)
async def list_hunts(
    registry: HuntConfigReg,
    user: CurrentUser,
    pagination: PaginationParams = Depends(),
    search: str | None = Query(
        None,
        description=(
            "Case-insensitive search in hunt_id, name, customers, rules, source_table, target_table"
        ),
    ),
    sort_by: str | None = Query(
        None,
        description="Sort field (hunt_id, name, source_table, target_table)",
    ),
    sort_order: str = Query("asc", description="Sort order: asc/desc"),
) -> PaginatedResponse[HuntSummary]:
    """List persisted hunt configurations with pagination and search."""
    raw = registry.list_hunts()
    raw = apply_search(
        raw,
        search,
        ["hunt_id", "name", "customers", "rules", "source_table", "target_table"],
    )
    raw = apply_sort(raw, sort_by, sort_order)
    summaries = [_hunt_row_to_summary(row) for row in raw]
    return PaginatedResponse.from_list(summaries, pagination.page, pagination.per_page)


@router.post(
    "",
    response_model=HuntDetailResponse,
    status_code=201,
    dependencies=[Depends(require_action(scopes_dict["hunt_write"]))],
)
async def create_hunt(
    body: HuntCreateRequest,
    user: CurrentUser,
    registry: HuntConfigReg,
) -> HuntDetailResponse:
    """Create a new hunt configuration YAML."""
    if registry.exists(body.hunt_id):
        raise HTTPException(
            status_code=409,
            detail={"code": "conflict", "message": f"Hunt '{body.hunt_id}' already exists"},
        )
    config = body.to_config_dict()
    registry.save(
        body.hunt_id,
        config,
        created_by=user.user_id,
        description=f"hunt: create {body.hunt_id}",
    )
    audit_resource_change(user.user_id, "hunt", body.hunt_id, "created")
    return HuntDetailResponse(hunt_id=body.hunt_id, config=config)


@router.get(
    "/{hunt_id}",
    response_model=HuntDetailResponse,
    dependencies=[Depends(require_action(scopes_dict["hunt_read"]))],
)
async def get_hunt(
    hunt_id: str,
    user: CurrentUser,
    registry: HuntConfigReg,
) -> HuntDetailResponse:
    """Get full hunt configuration by id."""
    try:
        config = registry.get(hunt_id)
    except HuntConfigNotFoundError:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Hunt '{hunt_id}' not found"},
        ) from None
    return HuntDetailResponse(hunt_id=hunt_id, config=config)


@router.put(
    "/{hunt_id}",
    response_model=HuntDetailResponse,
    dependencies=[Depends(require_action(scopes_dict["hunt_write"]))],
)
async def update_hunt(
    hunt_id: str,
    body: HuntWriteRequest,
    user: CurrentUser,
    registry: HuntConfigReg,
) -> HuntDetailResponse:
    """Replace an existing hunt configuration."""
    try:
        registry.get(hunt_id)
    except HuntConfigNotFoundError:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Hunt '{hunt_id}' not found"},
        ) from None

    config = body.to_config_dict()
    registry.save(
        hunt_id,
        config,
        created_by=user.user_id,
        description=f"hunt: update {hunt_id}",
    )
    audit_resource_change(user.user_id, "hunt", hunt_id, "updated")
    return HuntDetailResponse(hunt_id=hunt_id, config=config)


@router.delete(
    "/{hunt_id}",
    status_code=204,
    dependencies=[Depends(require_action(scopes_dict["hunt_delete"]))],
)
async def delete_hunt(
    hunt_id: str,
    user: CurrentUser,
    registry: HuntConfigReg,
):
    """Delete a hunt configuration."""
    try:
        registry.delete(hunt_id)
    except HuntConfigNotFoundError:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Hunt '{hunt_id}' not found"},
        ) from None
    audit_resource_change(user.user_id, "hunt", hunt_id, "deleted")


@router.post(
    "/{hunt_id}/run",
    response_model=TriggerResponse,
    status_code=202,
    dependencies=[Depends(require_action(scopes_dict["hunt_execute"]))],
)
async def trigger_hunt(
    hunt_id: str,
    body: TriggerRequest,
    request: Request,
    user: CurrentUser,
    registry: HuntConfigReg,
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

    target_hunt = _find_engine_hunt(engine, hunt_id, registry)
    if target_hunt is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Hunt '{hunt_id}' not found in scheduler"},
        )

    manager = _get_task_manager(request)
    task_info = manager.submit(
        "hunt:execute",
        _execute_hunt,
        target_hunt,
        body.customer,
    )

    audit_resource_change(user.user_id, "hunt", hunt_id, "executed")
    return TriggerResponse(task_id=task_info.id, hunt_name=target_hunt.name)


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
