"""Rules router — RuleCreationService create + validate.

POST   /api/v1/rules          → Create rule via RuleCreationService
POST   /api/v1/rules/validate → Validate SQL/CEL without creating
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from dfe_engine.api.deps import CurrentUser, Settings, require_action
from dfe_engine.auth.audit import audit_resource_change

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


class RuleCreateResponse(BaseModel):
    rule: RuleResponse
    sanitize_summary: dict = Field(default_factory=dict)
    sql_errors: list[SqlValidationError] = Field(default_factory=list)
    cost_estimate: CostEstimate | None = None


# ── Endpoints ────────────────────────────────────────────────


@router.post(
    "",
    response_model=RuleCreateResponse,
    status_code=201,
    dependencies=[Depends(require_action("config:write"))],
)
async def create_rule(
    body: RuleCreateRequest,
    user: CurrentUser,
    settings: Settings,
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

    rule_data = result.rule
    cost = None
    if result.cost_estimate:
        ce = result.cost_estimate
        cost = CostEstimate(
            estimated_rows=getattr(ce, "estimated_rows", None),
            explain_plan=getattr(ce, "explain_plan", None),
            explain_duration_ms=getattr(ce, "explain_duration_ms", None),
            window_minutes=getattr(ce, "window_minutes", body.cost_window_minutes),
            warnings=getattr(ce, "warnings", []),
        )

    audit_resource_change(user.user_id, "rule", rule_id, "created")
    return RuleCreateResponse(
        rule=RuleResponse(
            rule_id=rule_data.rule_id,
            name=rule_data.name,
            severity=rule_data.severity,
            source_db=rule_data.source_db,
            source_table=rule_data.source_table,
            where_clause=rule_data.where_clause,
            cel_filter=rule_data.cel_filter,
            original_sql=rule_data.original_sql,
            hunt_name=rule_data.hunt_name,
            source=rule_data.source,
            warnings=rule_data.warnings,
            created_at=rule_data.created_at,
        ),
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


@router.post(
    "/validate",
    response_model=SqlValidationResponse,
    dependencies=[Depends(require_action("config:read"))],
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
