"""Rules router — RuleCreationService create + validate + CRUD.

GET    /api/v1/rules              → Paginated list (search)
POST   /api/v1/rules              → Create rule via RuleCreationService
GET    /api/v1/rules/{rule_id}    → Rule detail
PUT    /api/v1/rules/{rule_id}    → Update rule
DELETE /api/v1/rules/{rule_id}    → Delete rule
POST   /api/v1/rules/validate     → Validate SQL/CEL without creating
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from dfe_engine.api.deps import CurrentUser, RuleReg, Settings, require_action
from dfe_engine.api.pagination import PaginatedResponse, PaginationParams, apply_search, apply_sort
from dfe_engine.auth.audit import audit_resource_change
from dfe_engine.auth.rbac_scopes import scopes_dict
from dfe_engine.hunts.rule_registry import RuleNotFoundError

router = APIRouter(prefix="/rules", tags=["Rules"])


# ── Request/response models ───────────────────────────────────


class RuleCreateRequest(BaseModel):
    """Create a hunt rule via RuleCreationService."""

    name: str = Field(description="Human-readable rule name")
    severity: str = Field(default="medium", description="low|medium|high|critical")
    source_type: str = Field(
        default="raw",
        description="'raw' (plain SQL) or 'hyperdx' (HyperDX saved search format)",
    )
    user_sql: str = Field(description="User-authored SQL WHERE fragment")
    cel_filter: str | None = Field(default=None, description="CEL expression filter")
    hunt_name: str | None = Field(default=None, description="Parent hunt name")
    source: str | None = Field(default=None, description="Source label (e.g. windows_audit)")
    estimate_cost: bool = Field(default=False, description="Run EXPLAIN and estimate query cost")
    cost_window_minutes: int = Field(default=60, description="Window in minutes for cost estimate")


class RuleUpdateRequest(BaseModel):
    """Update an existing hunt rule (same shape as create)."""

    name: str = Field(description="Human-readable rule name")
    severity: str = Field(default="medium", description="low|medium|high|critical")
    source_type: str = Field(
        default="raw",
        description="'raw' (plain SQL) or 'hyperdx' (HyperDX saved search format)",
    )
    user_sql: str = Field(description="User-authored SQL WHERE fragment")
    cel_filter: str | None = Field(default=None, description="CEL expression filter")
    hunt_name: str | None = Field(default=None, description="Parent hunt name")
    source: str | None = Field(default=None, description="Source label (e.g. windows_audit)")
    estimate_cost: bool = Field(default=False, description="Run EXPLAIN and estimate query cost")
    cost_window_minutes: int = Field(default=60, description="Window in minutes for cost estimate")


class SqlValidationRequest(BaseModel):
    sql: str = Field(description="SQL WHERE fragment to validate")


class SqlValidationError(BaseModel):
    message: str
    position: int | None = None
    suggestion: str | None = None


class SqlValidationResponse(BaseModel):
    valid: bool
    errors: list[SqlValidationError] = Field(default_factory=list)


class CostEstimate(BaseModel):
    estimated_rows: int | None = None
    explain_plan: str | None = None
    explain_duration_ms: float | None = None
    window_minutes: int = 60
    warnings: list[str] = Field(default_factory=list)


class RuleResponse(BaseModel):
    rule_id: str
    name: str
    severity: str
    source_db: str | None = None
    source_table: str | None = None
    where_clause: str
    cel_filter: str | None = None
    original_sql: str
    hunt_name: str | None = None
    source: str | None = None
    warnings: list[str] = Field(default_factory=list)
    created_at: str


class RuleSummary(BaseModel):
    rule_id: str
    name: str
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


# ── Endpoints ────────────────────────────────────────────────


@router.get(
    "",
    response_model=PaginatedResponse[RuleSummary],
    dependencies=[Depends(require_action(scopes_dict["rule_read"]))],
)
async def list_rules(
    user: CurrentUser,
    registry: RuleReg,
    pagination: PaginationParams = Depends(),
    search: str | None = Query(
        None,
        description="Case-insensitive search in rule_id, name, source, hunt_name, severity",
    ),
    severity: str | None = Query(None, description="Filter by severity"),
    source: str | None = Query(None, description="Filter by source label"),
    sort_by: str | None = Query(
        None,
        description="Sort field (rule_id, name, severity, source, hunt_name, created_at)",
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

    raw = apply_search(raw, search, ["rule_id", "name", "source", "hunt_name", "severity"])
    raw = apply_sort(raw, sort_by, sort_order)

    summaries = [RuleSummary.model_validate(row) for row in raw]
    return PaginatedResponse.from_list(summaries, pagination.page, pagination.per_page)


@router.post(
    "",
    response_model=RuleCreateResponse,
    status_code=201,
    dependencies=[Depends(require_action(scopes_dict["rule_write"]))],
)
async def create_rule(
    body: RuleCreateRequest,
    user: CurrentUser,
    settings: Settings,
    registry: RuleReg,
):
    """Create a new hunt rule via RuleCreationService.

    The service sanitizes the SQL, applies CEL→SQL transpilation,
    validates column references, and optionally estimates query cost.
    """
    import uuid

    from dfe_engine.hunts.rule_creation_service import (
        RuleCreateRequest as SvcRequest,
    )
    from dfe_engine.hunts.rule_creation_service import (
        RuleCreationService,
    )
    from dfe_engine.settings import get_clickhouse_config

    service = RuleCreationService(ch_config=get_clickhouse_config(settings))
    rule_id = str(uuid.uuid4())

    svc_request = SvcRequest(
        name=body.name,
        severity=body.severity,
        source_type=body.source_type,
        user_sql=body.user_sql,
        cel_filter=body.cel_filter,
        hunt_name=body.hunt_name,
        source=body.source,
        estimate_cost=body.estimate_cost,
        cost_window_minutes=body.cost_window_minutes,
    )

    result = service.create_rule(svc_request, rule_id)
    registry.save(result.rule, created_by=user.user_id, description=f"rule: create {rule_id}")

    audit_resource_change(user.user_id, "rule", rule_id, "created")
    return _build_create_response(result, body.cost_window_minutes)


@router.post(
    "/validate",
    response_model=SqlValidationResponse,
    dependencies=[Depends(require_action(scopes_dict["rule_validate"]))],
)
async def validate_rule_sql(
    body: SqlValidationRequest,
    user: CurrentUser,
    settings: Settings,
):
    """Validate a SQL WHERE fragment without creating a rule.

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
    "/{rule_id}",
    response_model=RuleResponse,
    dependencies=[Depends(require_action(scopes_dict["rule_read"]))],
)
async def get_rule(rule_id: str, user: CurrentUser, registry: RuleReg):
    """Get a detection rule by ID."""
    try:
        rule = registry.get(rule_id)
    except RuleNotFoundError:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Rule '{rule_id}' not found"},
        ) from None
    return _rule_to_response(rule)


@router.put(
    "/{rule_id}",
    response_model=RuleCreateResponse,
    dependencies=[Depends(require_action(scopes_dict["rule_write"]))],
)
async def update_rule(
    rule_id: str,
    body: RuleUpdateRequest,
    user: CurrentUser,
    settings: Settings,
    registry: RuleReg,
):
    """Replace a detection rule (re-runs creation pipeline, preserves created_at)."""
    try:
        existing = registry.get(rule_id)
    except RuleNotFoundError:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Rule '{rule_id}' not found"},
        ) from None

    from dfe_engine.hunts.rule_creation_service import (
        RuleCreateRequest as SvcRequest,
    )
    from dfe_engine.hunts.rule_creation_service import (
        RuleCreationService,
    )
    from dfe_engine.settings import get_clickhouse_config

    service = RuleCreationService(ch_config=get_clickhouse_config(settings))
    svc_request = SvcRequest(
        name=body.name,
        severity=body.severity,
        source_type=body.source_type,
        user_sql=body.user_sql,
        cel_filter=body.cel_filter,
        hunt_name=body.hunt_name,
        source=body.source,
        estimate_cost=body.estimate_cost,
        cost_window_minutes=body.cost_window_minutes,
    )
    result = service.create_rule(svc_request, rule_id)
    updated = result.rule.model_copy(update={"created_at": existing.created_at})
    registry.save(updated, created_by=user.user_id, description=f"rule: update {rule_id}")
    audit_resource_change(user.user_id, "rule", rule_id, "updated")
    return _build_create_response(
        result.model_copy(update={"rule": updated}),
        body.cost_window_minutes,
    )


@router.delete(
    "/{rule_id}",
    status_code=204,
    dependencies=[Depends(require_action(scopes_dict["rule_delete"]))],
)
async def delete_rule(rule_id: str, user: CurrentUser, registry: RuleReg):
    """Delete a detection rule."""
    try:
        registry.delete(rule_id)
    except RuleNotFoundError:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Rule '{rule_id}' not found"},
        ) from None
    audit_resource_change(user.user_id, "rule", rule_id, "deleted")


# ── Helpers ──────────────────────────────────────────────────


def _rule_to_response(rule) -> RuleResponse:
    return RuleResponse(
        rule_id=rule.rule_id,
        name=rule.name,
        severity=rule.severity,
        source_db=rule.source_db,
        source_table=rule.source_table,
        where_clause=rule.where_clause,
        cel_filter=rule.cel_filter,
        original_sql=rule.original_sql,
        hunt_name=rule.hunt_name,
        source=rule.source,
        warnings=rule.warnings,
        created_at=rule.created_at,
    )


def _build_create_response(result, cost_window_minutes: int) -> RuleCreateResponse:
    rule_data = result.rule
    cost = None
    if result.cost_estimate:
        ce = result.cost_estimate
        cost = CostEstimate(
            estimated_rows=getattr(ce, "estimated_rows", None),
            explain_plan=getattr(ce, "explain_plan", None),
            explain_duration_ms=getattr(ce, "explain_duration_ms", None),
            window_minutes=getattr(ce, "window_minutes", cost_window_minutes),
            warnings=getattr(ce, "warnings", []),
        )

    return RuleCreateResponse(
        rule=_rule_to_response(rule_data),
        sanitize_summary=result.sanitize_summary or {},
        sql_errors=[
            SqlValidationError(
                message=e.message,
                position=getattr(e, "position", None),
                suggestion=getattr(e, "suggestion", None),
            )
            for e in (result.sql_errors or [])
        ],
        cost_estimate=cost,
    )
