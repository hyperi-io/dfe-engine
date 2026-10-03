"""Rules router -- RuleCreationService create + validate + CRUD.

GET    /api/v1/rules              -> Paginated list (search)
POST   /api/v1/rules              -> Create rule via RuleCreationService
GET    /api/v1/rules/{name}    -> Rule detail
PUT    /api/v1/rules/{name}    -> Update rule
DELETE /api/v1/rules/{name}    -> Delete rule
POST   /api/v1/rules/validate     -> Validate SQL/CEL without creating
"""

import functools
import re
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field, field_validator
from scalo.concurrency import run_blocking

from dfe_engine.api.deps import CurrentUser, HuntConfigReg, RuleReg, Settings, require_action
from dfe_engine.api.errors import ErrorCode, ErrorResponse
from dfe_engine.api.pagination import PaginatedResponse, PaginationParams, apply_search, apply_sort
from dfe_engine.api.review import apply_review_headers, review_audit_detail
from dfe_engine.api.write_turn import WRITE_TURN
from dfe_engine.auth.audit import audit_resource_change
from dfe_engine.auth.rbac_scopes import scopes_dict
from dfe_engine.git_identity import git_author
from dfe_engine.hunts.hunt_config_registry import default_display_name
from dfe_engine.hunts.rule_creation_service import CostEstimate
from dfe_engine.hunts.rule_guard import source_label
from dfe_engine.hunts.rule_registry import RuleNotFoundError

_RULE_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")

router = APIRouter(prefix="/rules", tags=["Rules"], dependencies=[WRITE_TURN])

_REFUSED_RULE_RESPONSES: dict[int | str, dict] = {
    422: {
        "model": ErrorResponse,
        "description": (
            "Refused, nothing written. The body failed validation (code validation_error). "
            "Or the hunt runner could not compile the rule -- its SQL does not parse as "
            "one SELECT, names no <db>.<table> source, names a source that is not a table "
            "name (letters, digits, '_' and '-'), has no WHERE to detect with, or "
            "calls a function that reads outside the row (url, s3, a dictionary) or sends "
            "it to another service (the ai* functions) "
            "(code invalid_sql, the errors in context.sql_errors). Or the rule matches "
            "every event in its source (code rule_matches_everything, the source and "
            "detection WHERE in context)."
        ),
    },
}


# -- Request/response models -----------------------------------


class _RuleWriteFields(BaseModel):
    display_name: str | None = Field(
        default=None,
        description="Human-readable label (defaults from ``name`` when omitted on create)",
    )
    severity: str = Field(default="medium", description="low|medium|high|critical")
    user_sql: str = Field(
        min_length=1,
        description=(
            "Full detection query: SELECT ... FROM <db>.<table> WHERE ... "
            "(the FROM names the source table)"
        ),
    )
    cel_filter: str | None = Field(default=None, description="CEL expression filter")
    hunt_name: str | None = Field(default=None, description="Parent hunt name")
    source: str | None = Field(default=None, description="Source label (e.g. windows_audit)")
    estimate_cost: bool = Field(
        default=True,
        description=(
            "Return the alert-volume preview's measurement as cost_estimate. The preview "
            "and its warnings on the rule follow the deployment's detection guard either way."
        ),
    )
    cost_window_minutes: int | None = Field(
        default=None,
        description=(
            "Lookback window in minutes the preview counts over. Absent or 0 uses the "
            "deployment's preview window."
        ),
    )


class RuleCreateRequest(_RuleWriteFields):
    """Create a hunt rule via RuleCreationService."""

    name: str = Field(description="Rule file name (YAML stem); must be unique")

    source_type: Literal["raw", "hyperdx"] = Field(
        default="raw",
        description="'raw' (plain SQL) or 'hyperdx' (HyperDX saved search format)",
    )

    @field_validator("name")
    @classmethod
    def _validate_rule_name(cls, v: str) -> str:
        if not _RULE_NAME_PATTERN.match(v):
            raise ValueError(
                f"Rule name '{v}' must match /^[a-zA-Z0-9_-]+$/ (letters, digits, _, -)"
            )
        return v


class RuleUpdateRequest(_RuleWriteFields):
    """Update an existing hunt rule (re-runs creation pipeline; ``source_type`` is not accepted)."""

    model_config = {"extra": "ignore"}


class SqlValidationRequest(BaseModel):
    sql: str = Field(description="Full SELECT ... FROM ... query to validate")


class SqlValidationError(BaseModel):
    message: str
    position: int | None = None
    suggestion: str | None = None


class SqlValidationResponse(BaseModel):
    valid: bool
    errors: list[SqlValidationError] = Field(default_factory=list)


class RuleResponse(BaseModel):
    name: str = Field(description="Rule file name (YAML stem)")
    display_name: str = Field(description="Human-readable rule label")
    severity: str
    source_db: str | None = None
    source_table: str | None = None
    where_clause: str
    cel_filter: str | None = None
    original_sql: str
    hunt_name: str | None = None
    source: str | None = None
    warnings: list[str] = Field(default_factory=list)
    sql_errors: list[SqlValidationError] = Field(default_factory=list)
    created_at: str


class RuleSummary(BaseModel):
    name: str = Field(description="Rule file name (YAML stem)")
    display_name: str
    severity: str
    source: str | None = None
    source_db: str | None = None
    source_table: str | None = None
    hunt_name: str | None = None
    created_at: str


class RuleCreateResponse(BaseModel):
    rule: RuleResponse
    sanitize_summary: dict = Field(default_factory=dict)
    sql_errors: list[SqlValidationError] = Field(default_factory=list)
    cost_estimate: CostEstimate | None = None


class RuleFromHyperdxRequest(BaseModel):
    """Create a hunt rule from the SQL a HyperDX view runs.

    HyperDX renders the view to ``raw_sql`` and sends it here. The create
    pipeline strips the UI meta (time bounds, LIMIT, ``__hdx_time_bucket``,
    SETTINGS) via the HyperDX sanitizer, and the engine derives a unique rule id
    from the saved-search name. The caller gets that id back and opens
    ``/rules/{id}`` -- no id to invent, no IndexedDB round-trip.
    """

    model_config = {"extra": "forbid"}

    raw_sql: str = Field(
        min_length=1,
        description="The ClickHouse SELECT the HyperDX view renders to",
    )
    saved_search_name: str | None = Field(
        default=None, description="HyperDX saved-search name; seeds the rule id and label"
    )
    severity: str = Field(default="medium", description="low|medium|high|critical")
    hunt_name: str | None = Field(default=None, description="Parent hunt name")
    source: str | None = Field(default=None, description="Source label (e.g. windows_audit)")


class RuleFromHyperdxResponse(BaseModel):
    id: str = Field(description="Created rule id (YAML stem); open at /rules/{id}")
    display_name: str
    sanitize_summary: dict = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    sql_errors: list[SqlValidationError] = Field(default_factory=list)


# -- Endpoints ------------------------------------------------


@router.get(
    "",
    response_model=PaginatedResponse[RuleSummary],
    dependencies=[Depends(require_action(scopes_dict["rule_read"]))],
)
def list_rules(
    user: CurrentUser,
    registry: RuleReg,
    pagination: PaginationParams = Depends(),
    search: str | None = Query(
        None,
        description="Case-insensitive search in name, display_name, source, hunt_name, severity",
    ),
    severity: str | None = Query(None, description="Filter by severity"),
    source: str | None = Query(None, description="Filter by source label"),
    sort_by: str | None = Query(
        None,
        description="Sort field (name, display_name, severity, source, hunt_name, created_at)",
    ),
    sort_order: str = Query("asc", description="Sort order: asc/desc"),
):
    """List detection rules with pagination and search."""
    raw = registry.list_rules()

    if severity is not None:
        sev = severity.lower()
        raw = [row for row in raw if row.get("severity", "").lower() == sev]
    if source is not None:
        raw = [row for row in raw if row.get("source") == source]

    raw = apply_search(raw, search, ["name", "display_name", "source", "hunt_name", "severity"])
    raw = apply_sort(raw, sort_by, sort_order)

    summaries = [RuleSummary.model_validate(row) for row in raw]
    return PaginatedResponse.from_list(summaries, pagination.page, pagination.per_page)


@router.post(
    "",
    response_model=RuleCreateResponse,
    status_code=201,
    responses=_REFUSED_RULE_RESPONSES,
    dependencies=[Depends(require_action(scopes_dict["rule_write"]))],
)
def create_rule(
    body: RuleCreateRequest,
    user: CurrentUser,
    settings: Settings,
    registry: RuleReg,
    response: Response,
):
    """Create a new hunt rule via RuleCreationService.

    The service sanitizes the SQL, applies CEL->SQL transpilation,
    validates column references, and optionally estimates query cost. A rule
    whose SQL the hunt runner could not compile is refused with 422.

    A production+team write is routed to a review branch instead of the branch the
    runner git-syncs, and the ``X-DFE-Review-Required`` header says so.
    """
    from dfe_engine.hunts.rule_creation_service import (
        RuleCreateRequest as SvcRequest,
    )
    from dfe_engine.hunts.rule_creation_service import (
        RuleCreationService,
    )
    from dfe_engine.settings import get_clickhouse_config

    if registry.name_exists(body.name):
        raise HTTPException(
            status_code=409,
            detail={"code": "conflict", "message": f"Rule '{body.name}' already exists"},
        )

    service = RuleCreationService(ch_config=get_clickhouse_config(settings), hunts=settings.hunts)
    display = (
        body.display_name if body.display_name is not None else default_display_name(body.name)
    )

    svc_request = SvcRequest(
        name=display,
        severity=body.severity,
        source_type=body.source_type,
        user_sql=body.user_sql,
        cel_filter=body.cel_filter,
        hunt_name=body.hunt_name,
        source=body.source,
        estimate_cost=body.estimate_cost,
        cost_window_minutes=body.cost_window_minutes,
    )

    result = service.create_rule(svc_request, body.name)
    _refuse_uncompilable(result)
    _refuse_matches_everything(result)
    outcome = registry.save(
        result.rule, created_by=git_author(user), description=f"rule: create {body.name}"
    )
    apply_review_headers(response, outcome)
    audit_resource_change(user.user_id, "rule", body.name, "created", review_audit_detail(outcome))
    return _build_create_response(result)


@router.post(
    "/from-hyperdx",
    response_model=RuleFromHyperdxResponse,
    status_code=201,
    responses=_REFUSED_RULE_RESPONSES,
    dependencies=[Depends(require_action(scopes_dict["rule_write"]))],
)
async def create_rule_from_hyperdx(
    body: RuleFromHyperdxRequest,
    user: CurrentUser,
    settings: Settings,
    registry: RuleReg,
    response: Response,
):
    """Create a hunt rule from the SQL a HyperDX view renders to.

    Wraps the create pipeline with ``source_type='hyperdx'`` and auto-derives a
    unique rule id from the saved-search name, so the caller need not supply one.
    A view whose SQL the hunt runner could not compile, or that matches every
    event, is refused with 422. RBAC: ``rule:write`` (data_analyst) --
    ``org_viewer`` has neither this grant nor the UI button.
    """
    return await run_blocking(
        functools.partial(_create_rule_from_sql, body, user, settings, registry, response)
    )


def _create_rule_from_sql(
    body: RuleFromHyperdxRequest,
    user: CurrentUser,
    settings: Settings,
    registry: RuleReg,
    response: Response,
) -> RuleFromHyperdxResponse:
    """The deploy-repo and ClickHouse half of ``create_rule_from_hyperdx``, on a worker thread."""
    from dfe_engine.hunts.rule_creation_service import (
        RuleCreateRequest as SvcRequest,
    )
    from dfe_engine.hunts.rule_creation_service import (
        RuleCreationService,
    )
    from dfe_engine.settings import get_clickhouse_config

    rule_id = _unique_rule_id(registry, body.saved_search_name)
    display = body.saved_search_name or default_display_name(rule_id)

    service = RuleCreationService(ch_config=get_clickhouse_config(settings), hunts=settings.hunts)
    svc_request = SvcRequest(
        name=display,
        severity=body.severity,
        source_type="hyperdx",
        user_sql=body.raw_sql,
        hunt_name=body.hunt_name,
        source=body.source,
    )

    result = service.create_rule(svc_request, rule_id)
    _refuse_uncompilable(result)
    _refuse_matches_everything(result)
    outcome = registry.save(
        result.rule,
        created_by=git_author(user),
        description=f"rule: create {rule_id} (from hyperdx view)",
    )
    apply_review_headers(response, outcome)
    audit_resource_change(user.user_id, "rule", rule_id, "created", review_audit_detail(outcome))
    return RuleFromHyperdxResponse(
        id=rule_id,
        display_name=result.rule.name,
        sanitize_summary=result.sanitize_summary or {},
        warnings=list(result.rule.warnings),
        sql_errors=_map_sql_errors(result.sql_errors or []),
    )


@router.post(
    "/validate",
    response_model=SqlValidationResponse,
    dependencies=[Depends(require_action(scopes_dict["rule_validate"]))],
)
def validate_rule_sql(
    body: SqlValidationRequest,
    user: CurrentUser,
    settings: Settings,
):
    """Validate a detection query (SELECT ... FROM ...) without creating a rule.

    Returns any syntax errors or validation issues found.
    """
    from dfe_engine.hunts.rule_creation_service import RuleCreationService
    from dfe_engine.settings import get_clickhouse_config

    service = RuleCreationService(ch_config=get_clickhouse_config(settings))
    errors = service.validate_sql(body.sql)

    return SqlValidationResponse(
        valid=len(errors) == 0,
        errors=[
            SqlValidationError(
                message=e.message,
                position=getattr(e, "position", None),
                suggestion=getattr(e, "suggestion", None),
            )
            for e in errors
        ],
    )


@router.get(
    "/{name}",
    response_model=RuleResponse,
    dependencies=[Depends(require_action(scopes_dict["rule_read"]))],
)
def get_rule(name: str, user: CurrentUser, registry: RuleReg, settings: Settings):
    """Get a detection rule by file name."""
    try:
        rule = registry.get(name)
    except RuleNotFoundError:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Rule '{name}' not found"},
        ) from None
    sql_errors = _sql_errors_for_original_sql(settings, rule.original_sql)
    return _rule_to_response(rule, sql_errors=sql_errors)


@router.put(
    "/{name}",
    response_model=RuleCreateResponse,
    responses=_REFUSED_RULE_RESPONSES,
    dependencies=[Depends(require_action(scopes_dict["rule_write"]))],
)
def update_rule(
    name: str,
    body: RuleUpdateRequest,
    user: CurrentUser,
    settings: Settings,
    registry: RuleReg,
    response: Response,
):
    """Replace a detection rule (re-runs creation pipeline, preserves created_at).

    A replacement the hunt runner could not compile is refused with 422 and the
    stored rule is left as it was. A production+team write is routed to a review
    branch instead of the branch the runner git-syncs, and the
    ``X-DFE-Review-Required`` header says so.
    """
    try:
        existing = registry.get(name)
    except RuleNotFoundError:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Rule '{name}' not found"},
        ) from None

    from dfe_engine.hunts.rule_creation_service import (
        RuleCreateRequest as SvcRequest,
    )
    from dfe_engine.hunts.rule_creation_service import (
        RuleCreationService,
    )
    from dfe_engine.settings import get_clickhouse_config

    service = RuleCreationService(ch_config=get_clickhouse_config(settings), hunts=settings.hunts)
    effective_display = body.display_name if body.display_name is not None else existing.name
    effective_source = body.source if body.source is not None else existing.source
    effective_hunt = body.hunt_name if body.hunt_name is not None else existing.hunt_name
    svc_request = SvcRequest(
        name=effective_display,
        severity=body.severity,
        source_type="raw",
        user_sql=body.user_sql,
        cel_filter=body.cel_filter,
        hunt_name=effective_hunt,
        source=effective_source,
        estimate_cost=body.estimate_cost,
        cost_window_minutes=body.cost_window_minutes,
    )
    result = service.create_rule(svc_request, name)
    _refuse_uncompilable(result)
    _refuse_matches_everything(result)
    updated = result.rule.model_copy(
        update={"created_at": existing.created_at, "name": effective_display},
    )
    outcome = registry.save(
        updated, created_by=git_author(user), description=f"rule: update {name}"
    )
    apply_review_headers(response, outcome)
    audit_resource_change(user.user_id, "rule", name, "updated", review_audit_detail(outcome))
    return _build_create_response(result.model_copy(update={"rule": updated}))


@router.delete(
    "/{name}",
    status_code=204,
    dependencies=[Depends(require_action(scopes_dict["rule_delete"]))],
)
def delete_rule(
    name: str,
    user: CurrentUser,
    registry: RuleReg,
    hunt_registry: HuntConfigReg,
    response: Response,
):
    """Delete a detection rule.

    A production+team delete is routed to a review branch, so the runner keeps
    compiling the rule until that branch is merged; ``X-DFE-Review-Required`` says so.
    """
    used_by = hunt_registry.hunt_names_referencing_rule(name)
    if used_by:
        hunts = ", ".join(sorted(used_by))
        raise HTTPException(
            status_code=409,
            detail={
                "code": "conflict",
                "message": f"Rule '{name}' is referenced by hunt(s): {hunts}",
            },
        )
    try:
        outcome = registry.delete(name, created_by=git_author(user))
    except RuleNotFoundError:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Rule '{name}' not found"},
        ) from None
    apply_review_headers(response, outcome)
    audit_resource_change(user.user_id, "rule", name, "deleted", review_audit_detail(outcome))


# -- Helpers --------------------------------------------------


def _sql_errors_for_original_sql(settings: Settings, original_sql: str) -> list[SqlValidationError]:
    if not original_sql.strip():
        return []
    from dfe_engine.hunts.rule_creation_service import RuleCreationService
    from dfe_engine.settings import get_clickhouse_config

    service = RuleCreationService(ch_config=get_clickhouse_config(settings))
    return _map_sql_errors(service.validate_sql(original_sql))


def _rule_to_response(rule, *, sql_errors: list[SqlValidationError] | None = None) -> RuleResponse:
    return RuleResponse(
        name=rule.rule_id,
        display_name=rule.name,
        severity=rule.severity,
        source_db=rule.source_db,
        source_table=rule.source_table,
        where_clause=rule.where_clause,
        cel_filter=rule.cel_filter,
        original_sql=rule.original_sql,
        hunt_name=rule.hunt_name,
        source=rule.source,
        warnings=rule.warnings,
        sql_errors=sql_errors or [],
        created_at=rule.created_at,
    )


def _map_sql_errors(errors) -> list[SqlValidationError]:
    return [
        SqlValidationError(
            message=e.message,
            position=getattr(e, "position", None),
            suggestion=getattr(e, "suggestion", None),
        )
        for e in errors
    ]


def _refuse_uncompilable(result) -> None:
    """Refuse, before anything is written, a rule the hunt runner cannot compile.

    SQL that does not validate is refused on its SQL errors. SQL that validates
    can still leave nothing to run, such as a search with no filter, which the
    rule's own execution check reports.

    Raises:
        HTTPException: 422 ``invalid_sql``, every error in ``context.sql_errors``.
    """
    sql_errors = _map_sql_errors(result.sql_errors or [])
    if not sql_errors:
        sql_errors = [SqlValidationError(message=m) for m in result.rule.validate_rule()]
    if not sql_errors:
        return
    raise HTTPException(
        status_code=422,
        detail={
            "code": ErrorCode.INVALID_SQL,
            "message": "Rule not saved: " + "; ".join(e.message for e in sql_errors),
            "sql_errors": [e.model_dump(exclude_none=True) for e in sql_errors],
        },
    )


def _refuse_matches_everything(result) -> None:
    """Refuse, before anything is written, a rule whose condition matches every event.

    Raises:
        HTTPException: 422 ``rule_matches_everything``, with the source and the
            detection WHERE in context.
    """
    if not result.matches_everything:
        return
    raise HTTPException(
        status_code=422,
        detail={
            "code": ErrorCode.RULE_MATCHES_EVERYTHING,
            "message": result.matches_everything,
            "source": source_label(result.rule),
            "where_clause": result.rule.where_clause,
        },
    )


def _slugify_rule_id(text: str | None) -> str:
    """Slug free text into the rule-id charset /^[a-zA-Z0-9_-]+$/, falling back to a default."""
    base = re.sub(r"[^a-zA-Z0-9_-]+", "-", (text or "").strip()).strip("-").lower()
    return base or "hyperdx-rule"


def _unique_rule_id(registry, saved_search_name: str | None) -> str:
    """Derive a rule id from the saved-search name, de-duplicated against the registry."""
    base = _slugify_rule_id(saved_search_name)
    candidate = base
    n = 2
    while registry.name_exists(candidate):
        candidate = f"{base}-{n}"
        n += 1
    return candidate


def _build_create_response(result) -> RuleCreateResponse:
    sql_errors = _map_sql_errors(result.sql_errors or [])
    return RuleCreateResponse(
        rule=_rule_to_response(result.rule, sql_errors=sql_errors),
        sanitize_summary=result.sanitize_summary or {},
        sql_errors=sql_errors,
        cost_estimate=result.cost_estimate,
    )
