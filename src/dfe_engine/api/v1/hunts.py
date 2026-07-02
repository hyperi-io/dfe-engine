#  Project:      dfe-engine
#  File:         src/dfe_engine/api/v1/hunts.py
#  Purpose:      REST API for hunt engine status, listing, and ad-hoc execution
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Hunts router — scheduling status + hunt config CRUD.

Hunts execute in the separate dfe-hunt-runner service (pull-based, coordinated via
ClickHouse), not in this API process. This router provides:
- Scheduling status (runner liveness via active ClickHouse leases)
- Hunt configuration CRUD (YAML under ``hunts.hunt_dir``)
- Paginated hunt list with search
- On-demand execution is not wired yet (POST /{name}/run -> 501)
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator

from dfe_engine.api.deps import CurrentUser, HuntConfigReg, OptionalAlertDestStore, require_action
from dfe_engine.api.pagination import PaginatedResponse, PaginationParams, apply_search, apply_sort
from dfe_engine.auth.audit import audit_resource_change
from dfe_engine.auth.rbac_scopes import scopes_dict
from dfe_engine.hunts.alert_hunt_link import delete_destinations_owned_by_hunt
from dfe_engine.hunts.hunt_config_registry import (
    HuntConfigNotFoundError,
    default_display_name,
    resolve_display_name,
)

_HUNT_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")


def _rules_from_stored(rules: Any) -> list[dict[str, Any]]:
    """Normalize on-disk hunt ``rules`` for ``HuntRuleEntry`` parsing."""
    if not isinstance(rules, list):
        return []
    out: list[dict[str, Any]] = []
    for entry in rules:
        if isinstance(entry, str):
            out.append({"rule_name": entry})
        elif isinstance(entry, dict):
            name = entry.get("rule_name")
            if isinstance(name, str) and name:
                out.append(dict(entry))
    return out


def _rules_to_yaml(rule_names: list[str]) -> list[dict[str, str]]:
    return [{"rule_name": name} for name in rule_names]


router = APIRouter(prefix="/hunts", tags=["hunts"])


# ── Response models ─────────────────────────────────────────


class HuntEngineStatus(BaseModel):
    """Current state of the background hunt scheduler."""

    running: bool = Field(description="Whether the scheduler thread is alive")
    hunt_count: int = Field(default=0, description="Number of loaded hunts across all schedulers")
    scheduling_mode: str = Field(default="", description="Scheduling mode (cron, adaptive)")


class HuntSummary(BaseModel):
    """Summary of a configured hunt."""

    name: str = Field(description="Hunt file name (YAML stem, unique)")
    display_name: str = Field(description="Human-readable hunt label")
    customer: str = Field(default="", description="First customer in config (legacy summary field)")
    customers: list[str] = Field(default_factory=list)
    cron: str | list[str] = Field(default="", description="Cron schedule expression(s)")
    rules: list[str] = Field(default_factory=list)
    source_table: str = Field(default="")
    target_table: str = Field(default="")


class HuntRuleEntry(BaseModel):
    """Per-rule hunt configuration as stored in YAML."""

    rule_name: str
    target_table_name: str | None = None
    source: str | None = None
    initial_checkpoint_lookback_minutes: int | None = None


class _HuntConfigFields(BaseModel):
    """Shared hunt config fields (write and detail differ on ``rules``)."""

    display_name: str | None = Field(
        default=None,
        description="Human-readable label (defaults from ``name`` when omitted on write)",
    )
    cron: str | list[str] = Field(description="Cron expression or list of expressions")
    log_buffer: int = Field(default=60, ge=1)
    global_target_table_name: str
    global_source_table_name: str | None = None
    customers: list[str] = Field(min_length=1)
    customer_filters: dict[str, Any] | None = None
    checkpoint_timestamp_field: str | None = None
    scheduling_mode: str | None = None
    min_interval_seconds: int | None = None
    explain_queries: bool | None = None


class HuntWriteRequest(_HuntConfigFields):
    """Hunt scheduler payload for create/update (without hunt file ``name``)."""

    rules: list[str] = Field(
        min_length=1,
        description="Hunt rule template names (``{name}.jinja2`` under the rule repo)",
    )

    @field_validator("rules", mode="before")
    @classmethod
    def _rules_must_be_names(cls, v: Any) -> Any:
        if not isinstance(v, list):
            return v
        for item in v:
            if isinstance(item, dict):
                raise ValueError(
                    "rules must be a list of rule name strings, not rule objects "
                    "(edit hunt YAML directly for per-rule overrides)"
                )
        return v

    @field_validator("rules")
    @classmethod
    def _rules_non_empty_names(cls, v: list[str]) -> list[str]:
        cleaned = [name.strip() for name in v]
        if any(not name for name in cleaned):
            raise ValueError("rule names must be non-empty strings")
        return cleaned

    def to_config_dict(self, *, hunt_name: str) -> dict[str, Any]:
        display = (
            self.display_name if self.display_name is not None else default_display_name(hunt_name)
        )
        data = self.model_dump(exclude_none=True, exclude={"display_name"})
        data["display_name"] = display
        data["rules"] = _rules_to_yaml(self.rules)
        return data


class HuntDetailResponse(_HuntConfigFields):
    """Full hunt configuration returned from GET/create/update."""

    name: str = Field(description="Hunt file name (YAML stem)")
    display_name: str = Field(description="Resolved human-readable label")
    rules: list[HuntRuleEntry] = Field(min_length=1)

    @classmethod
    def from_stored_config(cls, name: str, config: dict[str, Any]) -> HuntDetailResponse:
        payload = {k: v for k, v in config.items() if k not in ("hunt_id", "name")}
        payload["rules"] = _rules_from_stored(config.get("rules"))
        payload["display_name"] = resolve_display_name(config, name)
        return cls.model_validate({"name": name, **payload})


class HuntCreateRequest(HuntWriteRequest):
    name: str = Field(description="Hunt file name (YAML stem); must be unique")

    @field_validator("name")
    @classmethod
    def _validate_hunt_name(cls, v: str) -> str:
        if not _HUNT_NAME_PATTERN.match(v):
            raise ValueError(
                f"Hunt name '{v}' must match /^[a-zA-Z0-9_-]+$/ (letters, digits, _, -)"
            )
        return v


class TriggerRequest(BaseModel):
    """Request to trigger an ad-hoc hunt execution."""

    customer: str = Field(description="Customer/org ID to run the hunt for")


class TriggerResponse(BaseModel):
    """Response from triggering an ad-hoc hunt."""

    task_id: str = Field(description="Task ID for polling via /tasks/{task_id}")
    hunt_name: str = Field(description="Display name of the triggered hunt")


# ── Dependencies ────────────────────────────────────────────


def _active_hunt_leases(request: Request) -> int:
    """Best-effort count of hunts currently held by a runner (active CH leases).

    Hunt execution runs in the separate dfe-hunt-runner service and coordinates via
    ClickHouse (the hunt_lease table). The API surfaces liveness by counting active
    leases; it returns 0 (never errors) when ClickHouse is unreachable or the
    coordination table does not exist yet.
    """
    try:
        from dfe_engine.clickhouse.clickhouse_manager import ClickHouseManager
        from dfe_engine.settings import get_settings

        db = get_settings().clickhouse.effective_data_database
        client = ClickHouseManager.get_instance().get_clickhouse_client()
        rows = client.query(
            "SELECT countIf(lu > toInt64(now())) FROM ("
            f"SELECT hunt_id, argMax(lease_until, claimed) AS lu "
            f"FROM `{db}`.hunt_lease GROUP BY hunt_id)"
        ).result_rows
        return int(rows[0][0]) if rows else 0
    except Exception:
        return 0


def _hunt_row_to_summary(row: dict[str, Any]) -> HuntSummary:
    customers = row.get("customers") or []
    return HuntSummary(
        name=row["name"],
        display_name=row["display_name"],
        customer=customers[0] if customers else "",
        customers=customers,
        cron=row.get("cron", ""),
        rules=row.get("rules", []),
        source_table=row.get("source_table", ""),
        target_table=row.get("target_table", ""),
    )


# ── Endpoints ───────────────────────────────────────────────


@router.get(
    "/status",
    response_model=HuntEngineStatus,
    dependencies=[Depends(require_action(scopes_dict["hunt_read"]))],
)
async def get_engine_status(
    request: Request,
    user: CurrentUser,
    registry: HuntConfigReg,
) -> HuntEngineStatus:
    """Report hunt scheduling status.

    Hunts execute in the separate dfe-hunt-runner service (pull-based, coordinated
    via ClickHouse), not in this API process. ``running`` reflects whether any
    runner currently holds a hunt lease; ``hunt_count`` is the configured-hunt
    count.
    """
    active = _active_hunt_leases(request)
    return HuntEngineStatus(
        running=active > 0,
        hunt_count=len(registry.list_hunts()),
        scheduling_mode="pull",
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
            "Case-insensitive search in name, display_name, customers, rules, "
            "source_table, target_table"
        ),
    ),
    sort_by: str | None = Query(
        None,
        description="Sort field (name, display_name, source_table, target_table)",
    ),
    sort_order: str = Query("asc", description="Sort order: asc/desc"),
) -> PaginatedResponse[HuntSummary]:
    """List persisted hunt configurations with pagination and search."""
    raw = registry.list_hunts()
    raw = apply_search(
        raw,
        search,
        ["name", "display_name", "customers", "rules", "source_table", "target_table"],
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
    if registry.name_exists(body.name):
        raise HTTPException(
            status_code=409,
            detail={"code": "conflict", "message": f"Hunt '{body.name}' already exists"},
        )
    config = body.to_config_dict(hunt_name=body.name)
    registry.save(
        body.name,
        config,
        created_by=user.user_id,
        description=f"hunt: create {body.name}",
    )
    audit_resource_change(user.user_id, "hunt", body.name, "created")
    return HuntDetailResponse.from_stored_config(body.name, config)


@router.get(
    "/{name}",
    response_model=HuntDetailResponse,
    dependencies=[Depends(require_action(scopes_dict["hunt_read"]))],
)
async def get_hunt(
    name: str,
    user: CurrentUser,
    registry: HuntConfigReg,
) -> HuntDetailResponse:
    """Get full hunt configuration by file name."""
    try:
        config = registry.get(name)
    except HuntConfigNotFoundError:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Hunt '{name}' not found"},
        ) from None
    return HuntDetailResponse.from_stored_config(name, config)


@router.put(
    "/{name}",
    response_model=HuntDetailResponse,
    dependencies=[Depends(require_action(scopes_dict["hunt_write"]))],
)
async def update_hunt(
    name: str,
    body: HuntWriteRequest,
    user: CurrentUser,
    registry: HuntConfigReg,
) -> HuntDetailResponse:
    """Replace an existing hunt configuration."""
    try:
        registry.get(name)
    except HuntConfigNotFoundError:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Hunt '{name}' not found"},
        ) from None

    config = body.to_config_dict(hunt_name=name)
    registry.save(
        name,
        config,
        created_by=user.user_id,
        description=f"hunt: update {name}",
    )
    audit_resource_change(user.user_id, "hunt", name, "updated")
    return HuntDetailResponse.from_stored_config(name, config)


@router.delete(
    "/{name}",
    status_code=204,
    dependencies=[Depends(require_action(scopes_dict["hunt_delete"]))],
)
async def delete_hunt(
    name: str,
    user: CurrentUser,
    registry: HuntConfigReg,
    alert_store: OptionalAlertDestStore,
):
    """Delete a hunt configuration."""
    try:
        registry.get(name)
    except HuntConfigNotFoundError:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Hunt '{name}' not found"},
        ) from None
    if alert_store is not None:
        delete_destinations_owned_by_hunt(alert_store, registry, name)
    registry.delete(name)
    audit_resource_change(user.user_id, "hunt", name, "deleted")


@router.post(
    "/{name}/run",
    response_model=TriggerResponse,
    status_code=202,
    dependencies=[Depends(require_action(scopes_dict["hunt_execute"]))],
)
async def trigger_hunt(
    name: str,
    body: TriggerRequest,
    request: Request,
    user: CurrentUser,
    registry: HuntConfigReg,
) -> TriggerResponse:
    """Trigger an ad-hoc hunt execution.

    Hunts run in the separate dfe-hunt-runner service on their schedule. On-demand
    execution from the API (enqueue a one-shot fire the runner claims) is not wired
    yet, so this returns 501 after validating the hunt exists.
    """
    try:
        registry.get(name)
    except HuntConfigNotFoundError:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Hunt '{name}' not found"},
        ) from None
    raise HTTPException(
        status_code=501,
        detail={
            "code": "not_implemented",
            "message": (
                "On-demand hunt execution runs via the dfe-hunt-runner service; "
                "ad-hoc trigger from the API is not yet wired"
            ),
        },
    )
