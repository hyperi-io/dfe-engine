#  Project:      dfe-engine
#  File:         src/dfe_engine/api/v1/hunts.py
#  Purpose:      REST API for hunt engine status, listing, and ad-hoc execution
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Hunts router — scheduling status + hunt config CRUD + per-hunt run state.

Hunts execute in the separate dfe-hunt-runner service (pull-based, coordinated via
ClickHouse), not in this API process. This router provides:
- Scheduling status (runner liveness via active ClickHouse leases)
- Hunt configuration CRUD (YAML under ``hunts.hunt_dir``)
- Paginated hunt list with search, each row carrying its own run state
- On-demand execution: queue a fire the running runner claims on its next poll

Run state rides the LIST rows rather than a per-hunt endpoint. The page renders a
table of hunts, so a per-hunt endpoint would be one request per row for something
one query already answers for all of them.
"""

from __future__ import annotations

import re
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from jinja2 import Environment
from pydantic import BaseModel, Field, field_validator
from scalo.logger import logger

from dfe_engine.api.deps import (
    CurrentUser,
    HuntConfigReg,
    OptionalAlertDestRegistry,
    Settings,
    require_action,
)
from dfe_engine.api.pagination import PaginatedResponse, PaginationParams, apply_search, apply_sort
from dfe_engine.api.review import apply_review_headers, review_audit_detail
from dfe_engine.auth.audit import audit_resource_change
from dfe_engine.auth.rbac_scopes import scopes_dict
from dfe_engine.hunt_runner.run_status import RunStatus, live_runner_count, read_run_status
from dfe_engine.hunt_runner.spec_loader import interval_seconds_for
from dfe_engine.hunt_runner.spread import next_due
from dfe_engine.hunts.alert_hunt_link import delete_destinations_owned_by_hunt
from dfe_engine.hunts.hunt_config_registry import (
    HuntConfigNotFoundError,
    default_display_name,
    resolve_display_name,
)
from dfe_engine.hunts.validator import HuntValidator

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


def _validate_hunt_config(config: dict[str, Any], settings: Any) -> None:
    """Run the deep HuntValidator on an API create/update hunt config (422 on error).

    Pydantic only checks the request SHAPE; this closes the gap where API CRUD skipped
    rule-file-existence + Jinja2 rule-syntax + structural (source/customer) validation.
    Rule lookup uses the CONSUMED rules dir (hunts.rules_dir). checkpoint_timestamp_field
    is API-optional so it falls back to the setting then a default. source_registry is
    None (source-ref checks only warn), so an unknown source ref is not blocked here.
    """
    checkpoint_field = (
        config.get("checkpoint_timestamp_field")
        or settings.hunts.checkpoint_timestamp_field
        or "timestamp"
    )
    try:
        HuntValidator.validate_hunt_configuration(
            config,
            Environment(),  # parses the Jinja2 rule templates (SQL, not HTML)
            settings.hunts.rules_dir,
            checkpoint_field,
            source_registry=None,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "validation_error", "message": str(exc)},
        ) from exc


router = APIRouter(prefix="/hunts", tags=["hunts"])


# ── Response models ─────────────────────────────────────────


class HuntEngineStatus(BaseModel):
    """Current state of the background hunt scheduler."""

    running: bool = Field(description="Whether at least one hunt runner is alive")
    runners: int = Field(
        default=0, description="Hunt runners that beat within their last two polls"
    )
    hunt_count: int = Field(default=0, description="Number of loaded hunts across all schedulers")
    scheduling_mode: str = Field(default="", description="Scheduling mode (cron, adaptive)")


class HuntSummary(BaseModel):
    """Summary of a configured hunt, with what the runner has done with it."""

    name: str = Field(description="Hunt file name (YAML stem, unique)")
    display_name: str = Field(description="Human-readable hunt label")
    customer: str = Field(default="", description="First customer in config (legacy summary field)")
    customers: list[str] = Field(default_factory=list)
    cron: str | list[str] = Field(default="", description="Cron schedule expression(s)")
    rules: list[str] = Field(default_factory=list)
    source_table: str = Field(default="")
    target_table: str = Field(default="")
    last_run: int | None = Field(
        default=None,
        description="Epoch seconds of the last window that committed; null = never run",
    )
    running: bool = Field(
        default=False, description="A runner holds a live lease on this hunt right now"
    )
    last_run_rows: int | None = Field(
        default=None,
        description="Rows the last completed run wrote; null = no run recorded",
    )
    too_aggressive: bool = Field(
        default=False,
        description="The hunt's schedule is tighter than it can keep up with",
    )
    run_requested: bool = Field(
        default=False, description="An ad-hoc run is queued and not yet claimed"
    )
    next_due: int | None = Field(
        default=None,
        description="Epoch seconds of the next scheduled fire; null = no rate schedule",
    )


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
    global_target_table_name: str | None = Field(
        default=None,
        description="Results table override; the rule's query carries its own target when omitted",
    )
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


class HuntRunQueued(BaseModel):
    """Response from queueing an ad-hoc hunt run."""

    hunt_name: str = Field(description="Hunt file name the run was queued for")
    queued: bool = Field(default=True, description="The run is recorded and waiting to be claimed")
    requested_fire: int = Field(description="Epoch seconds the run was queued at")
    poll_seconds: float = Field(
        description="How often a runner looks for work, so the longest wait before it starts"
    )


# ── Dependencies ────────────────────────────────────────────


def _data_database_client() -> tuple[Any, str]:
    """The ClickHouse client and data database the coordination tables live in."""
    from dfe_engine.clickhouse.clickhouse_manager import ClickHouseManager
    from dfe_engine.settings import get_settings

    return (
        ClickHouseManager.get_instance().get_clickhouse_client(),
        get_settings().clickhouse.effective_data_database,
    )


def _clickhouse_unreachable(exc: BaseException) -> bool:
    """True only when *exc* means ClickHouse could not be reached.

    A CONNECTION outage, or scalo's ServiceUnavailable once the reconnect budget is
    spent. Everything else -- a query error, a missing table, a bug in this process
    -- is NOT a reachability problem and must not be reported as one.
    """
    from scalo.resilience import ServiceUnavailable

    from dfe_engine.clickhouse.errors import is_connection_error

    return isinstance(exc, ServiceUnavailable) or is_connection_error(exc)


def _live_runners() -> int:
    """Best-effort count of hunt runners that have beaten within their last two polls.

    Hunt execution runs in the separate dfe-hunt-runner service and coordinates via
    ClickHouse; each runner writes a row to hunt_runner_heartbeat at the top of every
    tick. Returns 0 (never errors) when ClickHouse is unreachable or the coordination
    table does not exist yet.
    """
    try:
        client, db = _data_database_client()
        return live_runner_count(client, db, int(time.time()))
    except Exception:
        return 0


def _run_status_for(hunt_names: list[str], now: int) -> dict[str, RunStatus]:
    """Per-hunt run state, or an empty map when ClickHouse cannot answer.

    Best-effort on purpose: the hunt list is configuration, and it has to keep
    rendering when the data plane is down. A row then shows no run state rather
    than the whole page failing.
    """
    try:
        client, db = _data_database_client()
        return read_run_status(client, db, hunt_names, now)
    except Exception as exc:
        logger.warning(f"hunt run status unavailable: {exc}")
        return {}


def _next_due(row: dict[str, Any], now: int) -> int | None:
    """The hunt's next scheduled fire, from the same cron reading the runner uses."""
    interval = interval_seconds_for({"cron": row.get("cron")}, row["name"])
    if interval is None:
        return None
    return next_due(row["name"], interval, now)


def _hunt_row_to_summary(row: dict[str, Any], status: RunStatus | None, now: int) -> HuntSummary:
    customers = row.get("customers") or []
    # No status = the runner has never touched this hunt, or ClickHouse is down.
    run = status or RunStatus(hunt_id=row["name"])
    return HuntSummary(
        name=row["name"],
        display_name=row["display_name"],
        customer=customers[0] if customers else "",
        customers=customers,
        cron=row.get("cron", ""),
        rules=row.get("rules", []),
        source_table=row.get("source_table", ""),
        target_table=row.get("target_table", ""),
        last_run=run.last_run,
        running=run.running,
        last_run_rows=run.last_run_rows,
        too_aggressive=run.too_aggressive,
        run_requested=run.run_requested,
        next_due=_next_due(row, now),
    )


# ── Endpoints ───────────────────────────────────────────────


@router.get(
    "/status",
    response_model=HuntEngineStatus,
    dependencies=[Depends(require_action(scopes_dict["hunt_read"]))],
)
async def get_engine_status(
    user: CurrentUser,
    registry: HuntConfigReg,
) -> HuntEngineStatus:
    """Report hunt scheduling status.

    Hunts execute in the separate dfe-hunt-runner service (pull-based, coordinated
    via ClickHouse), not in this API process, so ``runners`` counts the runners that
    beat within their last two polls and ``running`` is whether any did. For "is this
    hunt running", read the per-hunt ``running`` on the hunts list; ``hunt_count`` is
    the configured-hunt count.
    """
    runners = _live_runners()
    return HuntEngineStatus(
        running=runners > 0,
        runners=runners,
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
    """List persisted hunt configurations, each with its run state, paginated.

    Run state comes from ONE ClickHouse read across every listed hunt: the watermark
    (last run), the lease (running now), hunt_state (too aggressive) and hunt_run
    (rows the last run wrote). Next due is derived, not stored.
    """
    raw = registry.list_hunts()
    raw = apply_search(
        raw,
        search,
        ["name", "display_name", "customers", "rules", "source_table", "target_table"],
    )
    raw = apply_sort(raw, sort_by, sort_order)
    now = int(time.time())
    status = _run_status_for([row["name"] for row in raw], now)
    summaries = [_hunt_row_to_summary(row, status.get(row["name"]), now) for row in raw]
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
    settings: Settings,
    response: Response,
) -> HuntDetailResponse:
    """Create a new hunt configuration YAML.

    A production+team write is routed to a review branch instead of the branch the
    runner git-syncs, and the ``X-DFE-Review-Required`` header says so.
    """
    if registry.name_exists(body.name):
        raise HTTPException(
            status_code=409,
            detail={"code": "conflict", "message": f"Hunt '{body.name}' already exists"},
        )
    config = body.to_config_dict(hunt_name=body.name)
    _validate_hunt_config(config, settings)
    outcome = registry.save(
        body.name,
        config,
        created_by=user.user_id,
        description=f"hunt: create {body.name}",
    )
    apply_review_headers(response, outcome)
    audit_resource_change(user.user_id, "hunt", body.name, "created", review_audit_detail(outcome))
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
    settings: Settings,
    response: Response,
) -> HuntDetailResponse:
    """Replace an existing hunt configuration.

    A production+team write is routed to a review branch instead of the branch the
    runner git-syncs, and the ``X-DFE-Review-Required`` header says so.
    """
    try:
        registry.get(name)
    except HuntConfigNotFoundError:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Hunt '{name}' not found"},
        ) from None

    config = body.to_config_dict(hunt_name=name)
    _validate_hunt_config(config, settings)
    outcome = registry.save(
        name,
        config,
        created_by=user.user_id,
        description=f"hunt: update {name}",
    )
    apply_review_headers(response, outcome)
    audit_resource_change(user.user_id, "hunt", name, "updated", review_audit_detail(outcome))
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
    alert_registry: OptionalAlertDestRegistry,
    response: Response,
):
    """Delete a hunt configuration.

    A production+team delete is routed to a review branch, so the runner keeps
    executing the hunt until that branch is merged; ``X-DFE-Review-Required`` says so.
    """
    try:
        registry.get(name)
    except HuntConfigNotFoundError:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Hunt '{name}' not found"},
        ) from None
    if alert_registry is not None:
        delete_destinations_owned_by_hunt(alert_registry, registry, name)
    outcome = registry.delete(name, created_by=user.user_id)
    apply_review_headers(response, outcome)
    audit_resource_change(user.user_id, "hunt", name, "deleted", review_audit_detail(outcome))


@router.post(
    "/{name}/run",
    response_model=HuntRunQueued,
    status_code=202,
    dependencies=[Depends(require_action(scopes_dict["hunt_execute"]))],
)
async def trigger_hunt(
    name: str,
    user: CurrentUser,
    registry: HuntConfigReg,
    settings: Settings,
) -> HuntRunQueued:
    """Queue an ad-hoc run: mark the hunt due now for the runner to claim.

    The pull model is kept. Nothing is pushed at the runner and no listener is added
    -- the fire is written into the coordination state, and the runner that is
    already running picks it up on its next poll, through the same claim that stops
    a hunt double-running. 202 says it is queued, not that it has run; the response
    carries the poll interval so the caller knows the longest wait before it starts.

    A hunt with no rate schedule is not something the runner ticks, so a queued run
    for one sits unclaimed. That is the schedule's shape, not a failure here.

    503 is reserved for ClickHouse actually being unreachable; any other failure to
    write the request is a 500, so a bug here never reads as an outage.
    """
    try:
        registry.get(name)
    except HuntConfigNotFoundError:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Hunt '{name}' not found"},
        ) from None

    from dfe_engine.hunt_runner.ch_coordinator import ChCoordinator

    fire = int(time.time())
    try:
        client, db = _data_database_client()
        ChCoordinator(client, database=db).request_run(name, fire)
    except Exception as exc:
        if not _clickhouse_unreachable(exc):
            # A 503 here would claim ClickHouse is down when it is not.
            logger.exception(f"could not queue a run for hunt '{name}': {exc}")
            raise HTTPException(
                status_code=500,
                detail={
                    "code": "queue_failed",
                    "message": (
                        "The run could not be queued and the cause was not a ClickHouse "
                        "connection failure; the error is in the engine log"
                    ),
                },
            ) from exc
        logger.error(f"could not queue a run for hunt '{name}': {exc}")
        raise HTTPException(
            status_code=503,
            detail={
                "code": "coordination_unavailable",
                "message": (
                    "The hunt coordination tables in ClickHouse are unreachable, "
                    "so the run could not be queued"
                ),
            },
        ) from exc

    audit_resource_change(user.user_id, "hunt", name, "run queued")
    return HuntRunQueued(
        hunt_name=name,
        requested_fire=fire,
        poll_seconds=settings.hunts.runner_poll_seconds,
    )
