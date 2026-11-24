#  Project:      dfe-engine
#  File:         src/dfe_engine/api/v1/hunts.py
#  Purpose:      REST API for hunt engine status, listing, and ad-hoc execution
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Hunts router -- scheduling status + hunt config CRUD + per-hunt run state.

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

import time
from collections.abc import Collection
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, Field, field_validator
from scalo.api import ListQuery, Page, ProblemError, list_query, paginate, sort_by_order
from scalo.logger import logger

from dfe_engine.api.conventions import apply_search
from dfe_engine.api.deps import (
    CurrentUser,
    HuntConfigReg,
    OptionalAlertDestRegistry,
    OptionalRuleReg,
    Settings,
    refuse_unknown_orgs,
    registered_orgs,
    require_action,
    require_platform_action,
)
from dfe_engine.api.errors import ErrorCode
from dfe_engine.api.review import apply_review_headers, review_audit_detail
from dfe_engine.api.write_turn import WRITE_TURN
from dfe_engine.auth.audit import audit_resource_change
from dfe_engine.auth.rbac_scopes import scopes_dict
from dfe_engine.clickhouse.quoting import plain_source_name
from dfe_engine.gitcrud.routing import WriteOutcome
from dfe_engine.hunts.alert_hunt_link import delete_destinations_owned_by_hunt
from dfe_engine.hunts.customers import CUSTOMER_TENANT_IDS, hunt_customers
from dfe_engine.hunts.hunt_config_registry import (
    HuntConfigNotFoundError,
    default_display_name,
    resolve_display_name,
)
from dfe_engine.hunts.hunt_target_check import resolve_target
from dfe_engine.hunts.rule_compiler import detection_clause
from dfe_engine.hunts.rule_creation_service import service_from_settings
from dfe_engine.hunts.rule_names import RuleNameError, missing_rules, rule_file
from dfe_engine.hunts.rule_rewriter import RuleRewriter
from dfe_engine.hunts.run_status import (
    RunStatus,
    clear_auto_disable,
    live_runner_count,
    read_run_status,
)
from dfe_engine.hunts.schedule import next_due, read_schedule
from dfe_engine.hunts.spec_loader import interval_seconds_for
from dfe_engine.hunts.validator import HuntValidator, UnknownRuleError, rule_source
from dfe_engine.names import HuntName, InvalidNameError, validate_rule_name
from dfe_engine.orgs.tenant_scope import tenant_ids_by_org
from dfe_engine.yaml_utils import YAMLError, yaml_load


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


def _held_rules(stored: dict[str, Any]) -> set[str]:
    """The rule names a stored hunt already names."""
    return {entry["rule_name"] for entry in _rules_from_stored(stored.get("rules"))}


def _refuse_new_rule_names(rules: list[str], stored: dict[str, Any]) -> None:
    """Refuse a rule reference no rule can be created under, unless *stored* already holds it.

    A hunt on disk can name a rule file outside the create rule, and it has to stay
    editable. The rule-file contract refuses a name that leaves the directory either way.

    Raises:
        ProblemError: 422 ``validation_error`` naming the first refused reference.
    """
    held = _held_rules(stored)
    for name in rules:
        if name in held:
            continue
        try:
            validate_rule_name(name)
        except InvalidNameError as exc:
            raise ProblemError(422, str(exc), code="validation_error") from exc


def _resolve_customers(request: Request, config: dict[str, Any]) -> None:
    """Store each customer's tenant ids beside the names, as the runner reads them.

    Raises:
        ProblemError: 422 ``unknown_org`` when a customer names no registered org.
    """
    customers = hunt_customers(config)
    refuse_unknown_orgs(request, customers)
    config[CUSTOMER_TENANT_IDS] = tenant_ids_by_org(customers, registered_orgs(request))


def _rules_dir(settings: Any, rules: Any) -> Any:
    """The directory the hunt runner reads rule files from: the registry's, else ``hunts.rules_dir``."""
    return rules.directory if rules is not None else settings.hunts.rules_dir


def _unreadable_rule_error(missing: list[str]) -> str:
    """The error a hunt carries while *missing* name no stored rule."""
    listed = ", ".join(f"'{name}'" for name in missing)
    return f"The runner cannot fire this hunt: no stored rule named {listed}"


def _validate_hunt_config(
    config: dict[str, Any], settings: Any, rules: Any, held: Collection[str] = ()
) -> None:
    """Run the deep HuntValidator on an API create/update hunt config (422 on error).

    Pydantic only checks the request SHAPE. This closes the gap where API CRUD skipped
    rule-file + structural (source/customer) validation. Each rule is looked up as the
    ``{name}.yaml`` the rule registry writes and the hunt runner reads: in the registry's
    directory (the deploy repo's ``config/rules`` under gitops), else ``hunts.rules_dir``.
    A rule with no file is refused, except one in *held*. checkpoint_timestamp_field is
    API-optional so it falls back to the setting then a default. source_registry is None
    (source-ref checks only warn), so an unknown source ref is not blocked here.
    """
    checkpoint_field = (
        config.get("checkpoint_timestamp_field")
        or settings.hunts.checkpoint_timestamp_field
        or "timestamp"
    )
    try:
        HuntValidator.validate_hunt_configuration(
            config,
            _rules_dir(settings, rules),
            checkpoint_field,
            source_registry=None,
            held_rules=held,
        )
    except UnknownRuleError as exc:
        listed = ", ".join(f"'{name}'" for name in exc.missing)
        raise ProblemError(422, f"Not a stored rule: {listed}", code="unknown_rule") from exc
    except ValueError as exc:
        raise ProblemError(422, str(exc), code="validation_error") from exc


def _stored_rule_errors(
    config: dict[str, Any], settings: Any, rules: Any, service: Any
) -> list[dict[str, Any]]:
    """Every error the runner would meet running each stored rule the hunt names.

    A rule with no file yet, or one the validator already refused, is left to it.
    """
    data_database = settings.clickhouse.effective_data_database
    rewriter = RuleRewriter()
    directory = _rules_dir(settings, rules)
    problems: list[dict[str, Any]] = []
    for entry in config.get("rules", []):
        name = str(entry["rule_name"])
        try:
            path = rule_file(directory, name, ".yaml")
            payload = yaml_load(path) if path.is_file() else None
        except RuleNameError, YAMLError, UnicodeDecodeError, OSError:
            continue
        if not isinstance(payload, dict):
            continue
        where, source_db, source_table = detection_clause(payload, rewriter)
        source = rule_source(source_db, source_table) or str(
            config.get("global_source_table_name") or ""
        )
        try:
            database, table = plain_source_name(source) if source.strip() else ("", "")
        except ValueError:
            continue
        for error in service.check_stored(database or data_database, table, where):
            entry_error = error.model_dump(exclude_none=True)
            entry_error["message"] = f"rule '{name}': {error.message}"
            problems.append(entry_error)
    return problems


def _check_hunt_will_run(config: dict[str, Any], settings: Any, rules: Any) -> None:
    """Refuse a hunt the runner could not run.

    Each stored rule passes the query check a save runs, and the results table is held to
    the columns the runner's INSERT names. Nothing is checked against ClickHouse where none
    is configured or reachable.

    Raises:
        ProblemError: 422 ``invalid_target`` for a table the INSERT does not fit, and 422
            ``invalid_sql`` for a rule ClickHouse cannot run.
    """
    database, table = resolve_target(config, settings.clickhouse.effective_data_database)
    service = service_from_settings(settings)
    reason = service.target_error(database, table)
    if reason is not None:
        raise ProblemError(422, f"Hunt not saved: {reason}", code=ErrorCode.INVALID_TARGET)
    problems = _stored_rule_errors(config, settings, rules, service)
    if problems:
        raise ProblemError(
            422,
            "Hunt not saved: " + "; ".join(p["message"] for p in problems),
            code=ErrorCode.INVALID_SQL,
            sql_errors=problems,
        )


router = APIRouter(prefix="/hunts", tags=["hunts"], dependencies=[WRITE_TURN])


# -- Response models -----------------------------------------


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
    enabled: bool = Field(
        default=True,
        description=(
            "Whether the runner fires this hunt. False keeps its YAML, state and watermark "
            "and fires nothing."
        ),
    )
    auto_disabled_at: int | None = Field(
        default=None,
        description=(
            "When the runner stopped the hunt after repeated resource-limit failures, epoch seconds"
        ),
    )
    auto_disabled_reason: str = Field(
        default="",
        description="The ClickHouse error category and code that tripped the auto-disable",
    )
    missing_rules: list[str] = Field(
        default_factory=list,
        description="Rules the hunt names that no stored rule file backs, so it cannot fire",
    )
    error: str = Field(
        default="",
        description="Why the runner cannot fire the hunt; empty when nothing stops it",
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
    customers: list[str] = Field(
        min_length=1,
        description=(
            "Names of the registered orgs the hunt reads. The engine resolves each to its "
            "tenant ids when it saves the hunt, and the runner reads no other org's rows."
        ),
    )
    checkpoint_timestamp_field: str | None = None
    scheduling_mode: str | None = None
    min_interval_seconds: int | None = None
    explain_queries: bool | None = None


class HuntWriteRequest(_HuntConfigFields):
    """Hunt scheduler payload for create/update (without hunt file ``name``)."""

    # The read model carries its own enabled flag: here None keeps the stored flag on
    # an update, so an edit of the cron never re-enables a hunt an operator stopped.
    enabled: bool | None = Field(
        default=None,
        description=(
            "Whether the runner fires this hunt. Omitted on an update keeps the stored "
            "value. True also clears a stop the runner put on the hunt."
        ),
    )
    rules: list[str] = Field(
        min_length=1,
        description="Rule names, each the ``{name}.yaml`` rule file the hunt runner reads",
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
        """The stored hunt config this request writes for *hunt_name*."""
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
    display_name: str = Field(description="Resolved human-readable label")  # ty: ignore[invalid-mutable-override]  # the response model narrows the field, never writes it
    enabled: bool = Field(
        default=True,
        description=(
            "Whether the runner fires this hunt. False keeps its YAML, state and watermark "
            "and fires nothing."
        ),
    )
    # Empty for a YAML hunt that runs a direct query, which the API never writes.
    rules: list[HuntRuleEntry] = Field(default_factory=list)
    query: str | None = Field(
        default=None,
        description="The SQL a YAML hunt runs in place of rules, read from the deploy repo",
    )
    # A stored hunt naming none is shown, and runs nothing.
    customers: list[str] = Field(
        default_factory=list, description="Names of the registered orgs the hunt reads"
    )
    customer_tenant_ids: dict[str, list[str]] = Field(  # noqa: V107
        default_factory=dict,
        description=(
            "Each customer's tenant ids, resolved when the hunt was saved: the _org_id "
            "values the runner reads. A customer missing here stops every run."
        ),
    )
    # The runner's stop rides on GET alone: a save answers with what it stored, and a
    # save naming enabled true has just cleared the stop.
    auto_disabled_at: int | None = Field(
        default=None,
        description="When the runner stopped the hunt after repeated resource-limit failures, epoch seconds",
    )
    auto_disabled_reason: str = Field(
        default="", description="The ClickHouse error category and code that tripped the stop"
    )
    missing_rules: list[str] = Field(
        default_factory=list,
        description="Rules the hunt names that no stored rule file backs, so it cannot fire",
    )
    error: str = Field(
        default="",
        description="Why the runner cannot fire the hunt; empty when nothing stops it",
    )

    @classmethod
    def from_stored_config(cls, name: str, config: dict[str, Any]) -> HuntDetailResponse:
        """Build the response from a stored hunt config."""
        payload = {k: v for k, v in config.items() if k not in ("hunt_id", "name")}
        payload["rules"] = _rules_from_stored(config.get("rules"))
        payload["display_name"] = resolve_display_name(config, name)
        stored_ids = config.get(CUSTOMER_TENANT_IDS)
        # A hand-written entry the runner would not read is shown as absent, not a 500.
        payload[CUSTOMER_TENANT_IDS] = (
            {
                customer: [i for i in ids if isinstance(i, str)]
                for customer, ids in stored_ids.items()
                if isinstance(customer, str) and isinstance(ids, list)
            }
            if isinstance(stored_ids, dict)
            else {}
        )
        return cls.model_validate({"name": name, **payload})


class HuntCreateRequest(HuntWriteRequest):
    """Body for creating a hunt under a new name."""

    name: HuntName = Field(description="Hunt file name (YAML stem); must be unique")

    @field_validator("rules")
    @classmethod
    def _rules_can_be_created(cls, v: list[str]) -> list[str]:
        for name in v:
            validate_rule_name(name)
        return v


class HuntRunQueued(BaseModel):
    """Response from queueing an ad-hoc hunt run."""

    hunt_name: str = Field(description="Hunt file name the run was queued for")
    queued: bool = Field(default=True, description="The run is recorded and waiting to be claimed")
    requested_fire: int = Field(description="Epoch seconds the run was queued at")
    poll_seconds: float = Field(
        description="How often a runner looks for work, so the longest wait before it starts"
    )


# -- Dependencies --------------------------------------------


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
    ClickHouse. Each runner writes a row to hunt_runner_heartbeat at the top of every
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


def _clear_auto_disable(name: str, *, enabled: bool | None, outcome: WriteOutcome | None) -> None:
    """Lift the runner's stop on a hunt when a direct save names ``enabled: true``.

    An edit that omits ``enabled`` leaves the stop where it is, and so does a save
    routed to a review branch, since nothing has changed on the branch the runner
    reads. The YAML is saved by now, so a failure is told to the caller as 503
    rather than answering 200 over a hunt that is still stopped.

    Raises:
        ProblemError: 503 ``auto_disable_not_cleared`` when ClickHouse did not take it.
    """
    if enabled is not True or (outcome is not None and outcome.review_required):
        return
    try:
        client, db = _data_database_client()
        run = read_run_status(client, db, [name], int(time.time())).get(name)
        if run is None or run.auto_disabled_at is None:
            return
        clear_auto_disable(client, db, name)
    except Exception as exc:
        raise ProblemError(
            503,
            f"Hunt '{name}' was saved, but the runner's stop on it could not be cleared: "
            f"{exc}. Repeat the request once ClickHouse answers.",
            code="auto_disable_not_cleared",
        ) from exc


def _enabled_flag(value: Any) -> bool:
    """A stored ``enabled`` as the flag the runner reads: absent is on, anything but True is off."""
    return value is None or value is True


def _learned_offsets(enabled: bool) -> dict[str, tuple[int, int]]:
    """The offsets ``hunt_schedule`` holds, read once per listing when smoothing is on.

    Best-effort like the run state: an unreadable table leaves every next_due on
    its hash offset rather than failing the page.
    """
    if not enabled:
        return {}
    try:
        client, db = _data_database_client()
        return read_schedule(client, db)
    except Exception as exc:
        logger.warning(f"hunt schedule offsets unavailable: {exc}")
        return {}


def _next_due(
    row: dict[str, Any], now: int, fraction: float, learned: dict[str, tuple[int, int]]
) -> int | None:
    """The hunt's next fire, from the same cron reading and offset source the runner uses.

    A learned offset applies only while the hunt's interval matches the one it was
    learned for, as in the runner.
    """
    interval = interval_seconds_for({"cron": row.get("cron")}, row["name"])
    if interval is None:
        return None
    offset = None
    if row["name"] in learned and learned[row["name"]][0] == interval:
        offset = learned[row["name"]][1]
    return next_due(row["name"], interval, now, fraction, offset)


def _hunt_row_to_summary(
    row: dict[str, Any],
    status: RunStatus | None,
    now: int,
    fraction: float,
    learned: dict[str, tuple[int, int]],
    rules_dir: Any,
) -> HuntSummary:
    customers = row.get("customers") or []
    missing = missing_rules(rules_dir, row.get("rules") or [])
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
        next_due=_next_due(row, now, fraction, learned),
        enabled=_enabled_flag(row.get("enabled")),
        auto_disabled_at=run.auto_disabled_at,
        auto_disabled_reason=run.auto_disabled_reason,
        missing_rules=missing,
        error=_unreadable_rule_error(missing) if missing else "",
    )


def _flag_missing_rules(detail: HuntDetailResponse, rules_dir: Any) -> HuntDetailResponse:
    """Set the error a hunt carries while a rule it names has no stored rule file."""
    missing = missing_rules(rules_dir, [entry.rule_name for entry in detail.rules])
    detail.missing_rules = missing
    detail.error = _unreadable_rule_error(missing) if missing else ""
    return detail


# -- Endpoints -----------------------------------------------


@router.get(
    "/status",
    response_model=HuntEngineStatus,
    dependencies=[Depends(require_action(scopes_dict["hunt_read"]))],
)
def get_engine_status(
    user: CurrentUser,
    registry: HuntConfigReg,
) -> HuntEngineStatus:
    """Report hunt scheduling status.

    Hunts execute in the separate dfe-hunt-runner service (pull-based, coordinated
    via ClickHouse), not in this API process, so ``runners`` counts the runners that
    beat within their last two polls and ``running`` is whether any did. For "is this
    hunt running", read the per-hunt ``running`` on the hunts list. ``hunt_count`` is
    the configured-hunt count.
    """
    runners = _live_runners()
    return HuntEngineStatus(
        running=runners > 0,
        runners=runners,
        hunt_count=len(registry.list_hunts()),
        scheduling_mode="pull",
    )


HuntsList = Annotated[
    ListQuery,
    Depends(
        list_query(
            order_by=("name", "display_name", "source_table", "target_table"), tiebreak="name"
        )
    ),
]


@router.get(
    "",
    response_model=Page[HuntSummary],
    dependencies=[Depends(require_platform_action(scopes_dict["hunt_read"]))],
)
def list_hunts(
    registry: HuntConfigReg,
    rules: OptionalRuleReg,
    user: CurrentUser,
    settings: Settings,
    query: HuntsList,
    search: str | None = Query(
        None,
        description=(
            "Case-insensitive search in name, display_name, customers, rules, "
            "source_table, target_table"
        ),
    ),
    rule: str | None = Query(
        None,
        description="Only the hunts whose rules include this rule, by its exact name",
    ),
) -> Page[HuntSummary]:
    """List persisted hunt configurations, each with its run state, paginated.

    Run state comes from ONE ClickHouse read across every listed hunt: the watermark
    (last run), the lease (running now), hunt_state (too aggressive) and hunt_run
    (rows the last run wrote). Next due is derived, not stored. A hunt naming a rule
    with no stored rule file carries ``missing_rules`` and an ``error``.
    """
    raw = registry.list_hunts()
    if rule is not None:
        raw = [row for row in raw if rule in (row.get("rules") or [])]
    raw = apply_search(
        raw,
        search,
        ["name", "display_name", "customers", "rules", "source_table", "target_table"],
    )
    raw = sort_by_order(raw, query.order_by)
    now = int(time.time())
    status = _run_status_for([row["name"] for row in raw], now)
    fraction = settings.hunts.phase_spread_fraction
    learned = _learned_offsets(settings.hunts.smoothing.enabled)
    rules_dir = _rules_dir(settings, rules)
    summaries = [
        _hunt_row_to_summary(row, status.get(row["name"]), now, fraction, learned, rules_dir)
        for row in raw
    ]
    return paginate(summaries, query, bound={"search": search, "rule": rule})


@router.post(
    "",
    response_model=HuntDetailResponse,
    status_code=201,
    dependencies=[Depends(require_action(scopes_dict["hunt_write"]))],
)
def create_hunt(
    body: HuntCreateRequest,
    user: CurrentUser,
    registry: HuntConfigReg,
    rules: OptionalRuleReg,
    settings: Settings,
    request: Request,
    response: Response,
) -> HuntDetailResponse:
    """Create a new hunt configuration YAML.

    Each rule the hunt names passes the query check a rule save runs, and the results
    table is checked against the columns the runner writes (422 ``invalid_sql``,
    ``invalid_target``).

    A production+team write is routed to a review branch instead of the branch the
    runner git-syncs, and the ``X-DFE-Review-Required`` header says so. 422
    ``unknown_org`` when a customer names no registered org.
    """
    if registry.name_exists(body.name):
        raise ProblemError(409, f"Hunt '{body.name}' already exists", code="conflict")
    config = body.to_config_dict(hunt_name=body.name)
    _validate_hunt_config(config, settings, rules)
    _check_hunt_will_run(config, settings, rules)
    _resolve_customers(request, config)
    outcome = registry.save(body.name, config, created_by=user.user_id)
    apply_review_headers(response, outcome)
    audit_resource_change(user.user_id, "hunt", body.name, "created", review_audit_detail(outcome))
    return _flag_missing_rules(
        HuntDetailResponse.from_stored_config(body.name, config), _rules_dir(settings, rules)
    )


@router.get(
    "/{name}",
    response_model=HuntDetailResponse,
    dependencies=[Depends(require_platform_action(scopes_dict["hunt_read"]))],
)
def get_hunt(
    name: str,
    user: CurrentUser,
    registry: HuntConfigReg,
    rules: OptionalRuleReg,
    settings: Settings,
) -> HuntDetailResponse:
    """Get full hunt configuration by file name, with the runner's stop on it if any.

    A hunt naming a rule with no stored rule file carries ``missing_rules`` and an ``error``.
    """
    try:
        config = registry.get(name)
    except HuntConfigNotFoundError:
        raise ProblemError(404, f"Hunt '{name}' not found", code="not_found") from None
    detail = _flag_missing_rules(
        HuntDetailResponse.from_stored_config(name, config), _rules_dir(settings, rules)
    )
    run = _run_status_for([name], int(time.time())).get(name)
    if run is not None:
        detail.auto_disabled_at = run.auto_disabled_at
        detail.auto_disabled_reason = run.auto_disabled_reason
    return detail


@router.put(
    "/{name}",
    response_model=HuntDetailResponse,
    dependencies=[Depends(require_action(scopes_dict["hunt_write"]))],
)
def update_hunt(
    name: str,
    body: HuntWriteRequest,
    user: CurrentUser,
    registry: HuntConfigReg,
    rules: OptionalRuleReg,
    settings: Settings,
    request: Request,
    response: Response,
) -> HuntDetailResponse:
    """Replace an existing hunt configuration, resolving its customers afresh.

    The rule and results-table checks of the create apply.

    A production+team write is routed to a review branch instead of the branch the
    runner git-syncs, and the ``X-DFE-Review-Required`` header says so. 422
    ``unknown_org`` when a customer names no registered org, 422 ``unknown_rule`` when a
    rule newly named has no stored rule file.
    """
    try:
        stored = registry.get(name)
    except HuntConfigNotFoundError:
        raise ProblemError(404, f"Hunt '{name}' not found", code="not_found") from None

    _refuse_new_rule_names(body.rules, stored)
    config = body.to_config_dict(hunt_name=name)
    if body.enabled is None and "enabled" in stored:
        config["enabled"] = stored["enabled"]
    _validate_hunt_config(config, settings, rules, _held_rules(stored))
    _check_hunt_will_run(config, settings, rules)
    _resolve_customers(request, config)
    outcome = registry.save(name, config, created_by=user.user_id)
    _clear_auto_disable(name, enabled=body.enabled, outcome=outcome)
    apply_review_headers(response, outcome)
    audit_resource_change(user.user_id, "hunt", name, "updated", review_audit_detail(outcome))
    return _flag_missing_rules(
        HuntDetailResponse.from_stored_config(name, config), _rules_dir(settings, rules)
    )


@router.delete(
    "/{name}",
    status_code=204,
    dependencies=[Depends(require_action(scopes_dict["hunt_delete"]))],
)
def delete_hunt(
    name: str,
    user: CurrentUser,
    registry: HuntConfigReg,
    alert_registry: OptionalAlertDestRegistry,
    response: Response,
):
    """Delete a hunt configuration.

    A production+team delete is routed to a review branch, so the runner keeps
    executing the hunt until that branch is merged. ``X-DFE-Review-Required`` says so.
    """
    try:
        registry.get(name)
    except HuntConfigNotFoundError:
        raise ProblemError(404, f"Hunt '{name}' not found", code="not_found") from None
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
def trigger_hunt(
    name: str,
    user: CurrentUser,
    registry: HuntConfigReg,
    settings: Settings,
) -> HuntRunQueued:
    """Queue an ad-hoc run: mark the hunt due now for the runner to claim.

    The pull model is kept. Nothing is pushed at the runner and no listener is added
    -- the fire is written into the coordination state, and the runner that is
    already running picks it up on its next poll, through the same claim that stops
    a hunt double-running. 202 says it is queued, not that it has run. The response
    carries the poll interval so the caller knows the longest wait before it starts.

    A hunt with no rate schedule is not something the runner ticks, so a queued run
    for one sits unclaimed. That is the schedule's shape, not a failure here.

    503 is reserved for ClickHouse actually being unreachable. Any other failure to
    write the request is a 500, so a bug here never reads as an outage.
    """
    try:
        registry.get(name)
    except HuntConfigNotFoundError:
        raise ProblemError(404, f"Hunt '{name}' not found", code="not_found") from None

    from dfe_engine.hunts.ch_coordinator import ChCoordinator

    fire = int(time.time())
    try:
        client, db = _data_database_client()
        ChCoordinator(client, database=db).request_run(name, fire)
    except Exception as exc:
        if not _clickhouse_unreachable(exc):
            # A 503 here would claim ClickHouse is down when it is not.
            logger.exception(f"could not queue a run for hunt '{name}': {exc}")
            raise ProblemError(
                500,
                "The run could not be queued and the cause was not a ClickHouse "
                "connection failure; the error is in the engine log",
                code="queue_failed",
            ) from exc
        logger.error(f"could not queue a run for hunt '{name}': {exc}")
        raise ProblemError(
            503,
            "The hunt coordination tables in ClickHouse are unreachable, "
            "so the run could not be queued",
            code="coordination_unavailable",
        ) from exc

    audit_resource_change(user.user_id, "hunt", name, "run queued")
    return HuntRunQueued(
        hunt_name=name,
        requested_fire=fire,
        poll_seconds=settings.hunts.runner_poll_seconds,
    )
