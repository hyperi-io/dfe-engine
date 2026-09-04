"""Rules router — RuleCreationService create + validate + CRUD.

GET    /api/v1/rules              → Paginated list (search)
POST   /api/v1/rules              → Create rule via RuleCreationService
GET    /api/v1/rules/{name}    → Rule detail
PUT    /api/v1/rules/{name}    → Update rule
DELETE /api/v1/rules/{name}    → Delete rule
POST   /api/v1/rules/validate     → Validate SQL/CEL without creating
"""

from __future__ import annotations

import re
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator, model_validator

from dfe_engine.api.deps import CurrentUser, HuntConfigReg, RuleReg, Settings, require_action
from dfe_engine.api.pagination import PaginatedResponse, PaginationParams, apply_search, apply_sort
from dfe_engine.auth.audit import audit_resource_change
from dfe_engine.auth.rbac_scopes import scopes_dict
from dfe_engine.git_identity import git_author
from dfe_engine.hunts.hunt_config_registry import default_display_name
from dfe_engine.hunts.rule_registry import RuleNotFoundError

_RULE_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")

router = APIRouter(prefix="/rules", tags=["Rules"])


# ── Request/response models ───────────────────────────────────


class _RuleWriteFields(BaseModel):
    display_name: str | None = Field(
        default=None,
        description="Human-readable label (defaults from ``name`` when omitted on create)",
    )
    severity: str = Field(default="medium", description="low|medium|high|critical")
    user_sql: str = Field(description="User-authored SQL WHERE fragment")
    cel_filter: str | None = Field(default=None, description="CEL expression filter")
    hunt_name: str | None = Field(default=None, description="Parent hunt name")
    source: str | None = Field(default=None, description="Source label (e.g. windows_audit)")
    estimate_cost: bool = Field(default=False, description="Run EXPLAIN and estimate query cost")
    cost_window_minutes: int = Field(default=60, description="Window in minutes for cost estimate")


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
    """Create a hunt rule from a live HyperDX view.

    Supply ``saved_search_id`` and the engine asks HyperDX what SQL that view
    actually runs. Supply ``raw_sql`` and the caller's string is taken on trust,
    which on a SQL-mode search is whatever sits in the editor rather than the
    query the view executes.

    Either way the create pipeline strips the UI meta (time bounds, LIMIT,
    ``__hdx_time_bucket``, SETTINGS) via the HyperDX sanitizer, and the engine
    derives a unique rule id from the saved-search name. The caller gets that id
    back and opens ``/rules/{id}`` -- no id to invent, no IndexedDB round-trip.
    """

    saved_search_id: str | None = Field(
        default=None,
        description="HyperDX saved-search id; the engine resolves the SQL that view runs",
    )
    raw_sql: str | None = Field(
        default=None,
        description="Pre-rendered ClickHouse SELECT, trusted as given",
    )
    saved_search_name: str | None = Field(
        default=None, description="HyperDX saved-search name; seeds the rule id and label"
    )
    severity: str = Field(default="medium", description="low|medium|high|critical")
    hunt_name: str | None = Field(default=None, description="Parent hunt name")
    source: str | None = Field(default=None, description="Source label (e.g. windows_audit)")

    @model_validator(mode="after")
    def _one_sql_source(self):
        """Require exactly one SQL source, so neither silently wins over the other."""
        if bool(self.saved_search_id) == bool(self.raw_sql):
            raise ValueError("supply exactly one of saved_search_id or raw_sql")
        return self


class RuleFromHyperdxResponse(BaseModel):
    id: str = Field(description="Created rule id (YAML stem); open at /rules/{id}")
    display_name: str
    resolved_from: Literal["saved_search", "raw_sql"] = Field(
        default="raw_sql", description="Which SQL source the rule was built from"
    )
    sanitize_summary: dict = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    sql_errors: list[SqlValidationError] = Field(default_factory=list)


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

    service = RuleCreationService(ch_config=get_clickhouse_config(settings))
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
    registry.save(result.rule, created_by=git_author(user), description=f"rule: create {body.name}")

    audit_resource_change(user.user_id, "rule", body.name, "created")
    return _build_create_response(result, body.cost_window_minutes)


@router.post(
    "/from-hyperdx",
    response_model=RuleFromHyperdxResponse,
    status_code=201,
    dependencies=[Depends(require_action(scopes_dict["rule_write"]))],
)
async def create_rule_from_hyperdx(
    body: RuleFromHyperdxRequest,
    request: Request,
    user: CurrentUser,
    settings: Settings,
    registry: RuleReg,
):
    """Create a hunt rule from a HyperDX view's expanded query.

    Wraps the create pipeline with ``source_type='hyperdx'`` and auto-derives a
    unique rule id from the saved-search name, so the caller need not supply one.
    RBAC: ``rule:write`` (data_analyst) -- ``org_viewer`` has neither this grant nor
    the UI button.
    """
    from dfe_engine.hunts.rule_creation_service import (
        RuleCreateRequest as SvcRequest,
    )
    from dfe_engine.hunts.rule_creation_service import (
        RuleCreationService,
    )
    from dfe_engine.settings import get_clickhouse_config

    raw_sql, search_name = await _resolve_hyperdx_sql(request, body)
    resolved_from = "raw_sql" if body.raw_sql else "saved_search"

    rule_id = _unique_rule_id(registry, search_name)
    display = search_name or default_display_name(rule_id)

    service = RuleCreationService(ch_config=get_clickhouse_config(settings))
    svc_request = SvcRequest(
        name=display,
        severity=body.severity,
        source_type="hyperdx",
        user_sql=raw_sql,
        hunt_name=body.hunt_name,
        source=body.source,
    )

    result = service.create_rule(svc_request, rule_id)
    registry.save(
        result.rule,
        created_by=git_author(user),
        description=f"rule: create {rule_id} (from hyperdx view)",
    )

    audit_resource_change(user.user_id, "rule", rule_id, "created")
    return RuleFromHyperdxResponse(
        id=rule_id,
        display_name=result.rule.name,
        resolved_from=resolved_from,
        sanitize_summary=result.sanitize_summary or {},
        warnings=list(getattr(result.rule, "warnings", []) or []),
        sql_errors=_map_sql_errors(result.sql_errors or []),
    )


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
    "/{name}",
    response_model=RuleResponse,
    dependencies=[Depends(require_action(scopes_dict["rule_read"]))],
)
async def get_rule(name: str, user: CurrentUser, registry: RuleReg, settings: Settings):
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
    dependencies=[Depends(require_action(scopes_dict["rule_write"]))],
)
async def update_rule(
    name: str,
    body: RuleUpdateRequest,
    user: CurrentUser,
    settings: Settings,
    registry: RuleReg,
):
    """Replace a detection rule (re-runs creation pipeline, preserves created_at)."""
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

    service = RuleCreationService(ch_config=get_clickhouse_config(settings))
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
    updated = result.rule.model_copy(
        update={"created_at": existing.created_at, "name": effective_display},
    )
    registry.save(updated, created_by=git_author(user), description=f"rule: update {name}")
    audit_resource_change(user.user_id, "rule", name, "updated")
    return _build_create_response(
        result.model_copy(update={"rule": updated}),
        body.cost_window_minutes,
    )


@router.delete(
    "/{name}",
    status_code=204,
    dependencies=[Depends(require_action(scopes_dict["rule_delete"]))],
)
async def delete_rule(
    name: str, user: CurrentUser, registry: RuleReg, hunt_registry: HuntConfigReg
):
    """Delete a detection rule."""
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
        registry.delete(name, created_by=git_author(user))
    except RuleNotFoundError:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Rule '{name}' not found"},
        ) from None
    audit_resource_change(user.user_id, "rule", name, "deleted")


# ── Helpers ──────────────────────────────────────────────────


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


async def _resolve_hyperdx_sql(
    request: Request, body: RuleFromHyperdxRequest
) -> tuple[str, str | None]:
    """Return the SQL to build the rule from, plus the saved-search name to label it.

    A saved-search id is resolved by ASKING HyperDX what that view runs, rather
    than re-rendering it here: the fork owns the chart-config renderer, and a
    second renderer would be a second definition of what the view means.
    """
    if body.raw_sql:
        return body.raw_sql, body.saved_search_name

    client = getattr(request.app.state, "hyperdx_client", None)
    if client is None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "hyperdx_unconfigured",
                "message": "saved_search_id needs a configured HyperDX; send raw_sql instead",
            },
        )

    rendered = await client.saved_search_sql(body.saved_search_id or "")
    sql = (rendered or {}).get("rawSql") or ""
    if not (sql):
        raise HTTPException(
            status_code=502,
            detail={
                "code": "hyperdx_render_failed",
                "message": (f"HyperDX returned no SQL for saved search {body.saved_search_id!r}"),
            },
        )
    return sql, body.saved_search_name or (rendered or {}).get("savedSearchName")


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

    sql_errors = _map_sql_errors(result.sql_errors or [])
    return RuleCreateResponse(
        rule=_rule_to_response(rule_data, sql_errors=sql_errors),
        sanitize_summary=result.sanitize_summary or {},
        sql_errors=sql_errors,
        cost_estimate=cost,
    )
