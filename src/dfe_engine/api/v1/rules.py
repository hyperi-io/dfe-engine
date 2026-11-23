"""Rules router -- a rule is a registered source plus the WHERE that selects its rows.

GET    /api/v1/rules              -> Paginated list (search)
POST   /api/v1/rules              -> Create a rule from a source and a WHERE clause
GET    /api/v1/rules/{name}    -> Rule detail
PUT    /api/v1/rules/{name}    -> Update rule
DELETE /api/v1/rules/{name}    -> Delete rule
POST   /api/v1/rules/check        -> Check a source and WHERE as a save would, saving nothing
POST   /api/v1/rules/preview      -> Compile a condition and check it as a save would, saving nothing
POST   /api/v1/rules/import       -> Read a SELECT into a source and WHERE, saving nothing
POST   /api/v1/rules/from-hyperdx -> Read the SQL a Search view runs into a source and WHERE
"""

import re
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, Field, model_validator
from scalo.api import ListQuery, Page, ProblemError, list_query, paginate, sort_by_order

from dfe_engine.api.conventions import apply_search
from dfe_engine.api.deps import (
    CurrentUser,
    HuntConfigReg,
    OptionalHuntConfigReg,
    OptionalSourceReg,
    RuleReg,
    Settings,
    SourceReg,
    held_tenant_ids,
    require_action,
)
from dfe_engine.api.errors import ErrorCode
from dfe_engine.api.review import apply_review_headers, review_audit_detail
from dfe_engine.api.write_turn import WRITE_TURN
from dfe_engine.auth.audit import audit_resource_change
from dfe_engine.auth.rbac_scopes import scopes_dict
from dfe_engine.git_identity import git_author
from dfe_engine.hunts.customers import hunt_tenant_ids
from dfe_engine.hunts.hunt_config_registry import HuntConfigNotFoundError, default_display_name
from dfe_engine.hunts.rule_condition import RuleCondition
from dfe_engine.hunts.rule_creation_service import CostEstimate, service_from_settings
from dfe_engine.hunts.rule_guard import source_label
from dfe_engine.hunts.rule_import import (
    DroppedPart,
    ImportRefusedError,
    RuleImport,
    import_select,
    sanitize_summary,
)
from dfe_engine.hunts.rule_model import Severity
from dfe_engine.hunts.rule_registry import RuleNotFoundError
from dfe_engine.hunts.rule_sources import resolved_source, source_check
from dfe_engine.names import MAX_NAME_LENGTH, RuleName

router = APIRouter(prefix="/rules", tags=["Rules"], dependencies=[WRITE_TURN])

_REFUSED_RULE_RESPONSES: dict[int | str, dict] = {
    422: {
        "description": (
            "Refused, nothing written. The body failed validation (code validation_error). "
            "Or the hunt runner could not compile the rule -- its source is not a registered "
            "source, its WHERE is empty, carries a {...} placeholder or a time bound, holds "
            "more than a row filter, calls a function that reads outside the row (url, s3, a "
            "dictionary) or sends it to another service (the ai* functions), is not SQL "
            "ClickHouse can plan against the source's table, or its CEL filter does not "
            "transpile (code invalid_sql, the errors in sql_errors). "
            "Or the rule matches "
            "every event in its source (code rule_matches_everything, the source and "
            "detection WHERE in context)."
        ),
    },
}

_IMPORT_REFUSED_RESPONSES: dict[int | str, dict] = {
    422: {
        "description": (
            "Refused. The body failed validation (code validation_error), or the SELECT has "
            "a shape a rule cannot carry: GROUP BY, HAVING, a JOIN, a UNION, a WITH clause "
            "or a subquery in FROM (code import_refused, the reason in detail)."
        ),
    },
}


# -- Request/response models -----------------------------------


class _RuleWriteFields(BaseModel):
    model_config = {"extra": "forbid"}

    display_name: str | None = Field(
        default=None,
        description="Human-readable label (defaults from ``name`` when omitted on create)",
    )
    severity: Severity = Field(default="medium", description="low|medium|high|critical")
    source: str = Field(
        min_length=1,
        description=(
            "The registered source whose table the rule reads, by its label "
            "(e.g. windows_audit). A label no source carries is refused"
        ),
    )
    where_clause: str | None = Field(
        default=None,
        description=(
            "The row filter, written as the part after WHERE, with no time bound and no "
            "{...} placeholder, since the time window comes from the hunt schedule. Give this "
            "or ``condition``, not both"
        ),
    )
    condition: RuleCondition | None = Field(
        default=None,
        description=(
            "The rule's structured condition, which the engine compiles to its WHERE and stores "
            "with it, so the builder reopens on it. Give this or ``where_clause``, not both"
        ),
    )
    cel_filter: str | None = Field(
        default=None, description="CEL expression ANDed into the WHERE, or the whole filter alone"
    )
    hunt_name: str | None = Field(default=None, description="Parent hunt name")
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

    @model_validator(mode="after")
    def _detection_is_written_once(self) -> _RuleWriteFields:
        if self.where_clause is not None and self.condition is not None:
            raise ValueError("give 'where_clause' or 'condition', not both")
        if self.where_clause is None and self.condition is None and not self.cel_filter:
            raise ValueError("give 'where_clause', 'condition' or 'cel_filter'")
        return self


class RuleCreateRequest(_RuleWriteFields):
    """Create a hunt rule from a registered source and a WHERE clause."""

    name: RuleName = Field(description="Rule file name (YAML stem); must be unique")


class RuleUpdateRequest(_RuleWriteFields):
    """Replace a hunt rule, re-running the creation checks on its source and WHERE."""

    name: str | None = Field(
        default=None, description="Accepted and ignored: the path names the rule"
    )


class RuleCheckRequest(BaseModel):
    """A source and WHERE clause to check as a save would, without saving them."""

    source: str = Field(description="The registered source whose table the rule reads")
    where_clause: str = Field(description="The row filter, as the part after WHERE")


class SqlValidationError(BaseModel):
    """One error in a rule, with where it is and a suggested fix."""

    message: str
    position: int | None = None
    suggestion: str | None = None


class RuleCheckResponse(BaseModel):
    """Whether a save of the source and WHERE would be accepted, and why not."""

    valid: bool
    errors: list[SqlValidationError] = Field(default_factory=list)
    warnings: list[str] = Field(
        default_factory=list,
        description=(
            "What a save would accept but the author should read: an alert-volume reading, "
            "a _json path recent rows of the source never carried"
        ),
    )


class RulePreviewRequest(BaseModel):
    """A condition to compile and check as a save would, without saving it."""

    source: str = Field(description="Source label whose table the condition reads")
    condition: RuleCondition
    cel_filter: str | None = Field(default=None, description="CEL expression ANDed in, as on save")
    cost_window_minutes: int | None = Field(
        default=None,
        description=(
            "Lookback window in minutes the volume preview counts over. Absent or 0 uses the "
            "deployment's preview window."
        ),
    )
    name: str | None = Field(
        default=None,
        description=(
            "The rule being edited. The volume preview then counts the orgs of the hunts "
            "that run it and of the hunt it names, as its save would"
        ),
    )
    hunt_name: str | None = Field(
        default=None,
        description="The hunt the rule names as its own, which the count follows. Defaults to the stored rule's",
    )


class RulePreviewResponse(BaseModel):
    """What a save of the condition would compile to and find, and nothing written."""

    user_sql: str = Field(
        description="The rule SQL the condition compiles to, empty when it does not"
    )
    source_db: str | None = None
    source_table: str | None = None
    where_clause: str = Field(description="The detection WHERE the rule would store")
    sql_errors: list[SqlValidationError] = Field(
        default_factory=list, description="Why a save would be refused as invalid"
    )
    matches_everything: str | None = Field(
        default=None,
        description="Why a save would be refused for matching every event, when it would",
    )
    warnings: list[str] = Field(
        default_factory=list,
        description="What a save would accept but the author should read, such as an unseen _json path",
    )
    cost_estimate: CostEstimate | None = None


class RuleResponse(BaseModel):
    """A detection rule as the API returns it."""

    name: str = Field(description="Rule file name (YAML stem)")
    display_name: str = Field(description="Human-readable rule label")
    severity: str
    source_db: str | None = None
    source_table: str | None = None
    where_clause: str
    cel_filter: str | None = None
    condition: RuleCondition | None = Field(
        default=None,
        description="The structured condition the SQL was compiled from, absent for a rule written as SQL",
    )
    original_sql: str
    hunt_name: str | None = None
    source: str | None = None
    warnings: list[str] = Field(default_factory=list)
    sql_errors: list[SqlValidationError] = Field(default_factory=list)
    created_at: str


class RuleSummary(BaseModel):
    """A detection rule in a listing."""

    name: str = Field(description="Rule file name (YAML stem)")
    display_name: str
    severity: str
    source: str | None = None
    source_db: str | None = None
    source_table: str | None = None
    hunt_name: str | None = None
    created_at: str


class RuleCreateResponse(BaseModel):
    """The created rule and what it is estimated to cost."""

    rule: RuleResponse
    sql_errors: list[SqlValidationError] = Field(default_factory=list)
    cost_estimate: CostEstimate | None = None


class RuleImportRequest(BaseModel):
    """A SELECT to read into a source and a WHERE clause."""

    model_config = {"extra": "forbid"}

    sql: str = Field(min_length=1, description="One ClickHouse SELECT of a single table")


class DroppedPartResponse(BaseModel):
    """One piece of the SELECT the rule does not keep."""

    kind: str = Field(
        description=(
            "columns, order_by, limit, offset, settings, format, distinct, time_bound "
            "or time_placeholder"
        )
    )
    text: str = Field(description="The piece as written")


class RuleImportResponse(BaseModel):
    """What a SELECT becomes as a rule's fields. Nothing is saved."""

    source: str | None = Field(
        default=None,
        description="The registered source the FROM names. Null when the author must pick one",
    )
    needs_source: bool = Field(  # noqa: V107
        description="True when the author must pick a source to save"
    )
    from_table: str = Field(description="The FROM as written, or the placeholders it was")
    where_clause: str = Field(
        description="The row filter with the time window removed, empty when nothing is left"
    )
    dropped: list[DroppedPartResponse] = Field(
        default_factory=list, description="Everything in the SELECT the rule does not keep"
    )
    warnings: list[str] = Field(
        default_factory=list, description="What to look at before saving the fields"
    )


class RuleFromHyperdxRequest(BaseModel):
    """The SQL a Search view runs, to read into the fields of a rule editor."""

    model_config = {"extra": "forbid"}

    raw_sql: str = Field(
        min_length=1,
        description="The ClickHouse SELECT the Search view renders to",
    )
    saved_search_name: str | None = Field(
        default=None, description="Search saved-search name; seeds the rule name and label"
    )
    severity: Severity = Field(default="medium", description="low|medium|high|critical")
    hunt_name: str | None = Field(default=None, description="Parent hunt name")
    source: str | None = Field(
        default=None,
        description="The source the view reads, which wins over the FROM when it is registered",
    )


class RuleFromHyperdxResponse(RuleImportResponse):
    """The fields a rule editor opens pre-filled with. Nothing is saved."""

    name: str = Field(description="A free rule name drawn from the saved-search name")
    display_name: str
    severity: str
    hunt_name: str | None = None
    sanitize_summary: dict = Field(default_factory=dict)


# -- Endpoints ------------------------------------------------


RulesList = Annotated[
    ListQuery,
    Depends(
        list_query(
            order_by=("name", "display_name", "severity", "source", "hunt_name", "created_at"),
            tiebreak="name",
        )
    ),
]


@router.get(
    "",
    response_model=Page[RuleSummary],
    dependencies=[Depends(require_action(scopes_dict["rule_read"]))],
)
def list_rules(
    user: CurrentUser,
    registry: RuleReg,
    sources: OptionalSourceReg,
    query: RulesList,
    search: str | None = Query(
        None,
        description="Case-insensitive search in name, display_name, source, hunt_name, severity",
    ),
    severity: str | None = Query(None, description="Filter by severity"),
    source: str | None = Query(None, description="Filter by source label"),
):
    """List detection rules with pagination and search.

    A stored rule with no registered source whose table is a registered source's label is
    listed under that source. One that names none lists with its stored source as it is.
    """
    raw = _with_resolved_sources(registry.list_rules(), sources)

    if severity is not None:
        sev = severity.lower()
        raw = [row for row in raw if row.get("severity", "").lower() == sev]
    if source is not None:
        raw = [row for row in raw if row.get("source") == source]

    raw = apply_search(raw, search, ["name", "display_name", "source", "hunt_name", "severity"])
    raw = sort_by_order(raw, query.order_by)

    summaries = [RuleSummary.model_validate(row) for row in raw]
    return paginate(
        summaries, query, bound={"search": search, "severity": severity, "source": source}
    )


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
    sources: SourceReg,
    request: Request,
    response: Response,
    hunts: OptionalHuntConfigReg,
):
    """Create a new hunt rule from a registered source and a WHERE clause.

    The rule is written as a ``where_clause`` or as a structured ``condition``, which
    the engine compiles to the WHERE and stores with it. A CEL filter is ANDed in, or
    stands alone. The service checks the source is registered, reads the WHERE as the
    hunt runner will, has ClickHouse plan it against the source's table, and optionally
    estimates alert volume. A rule the hunt runner could not compile is refused with 422.
    A ``_json`` path the source's recent rows never carried comes back as a warning. The
    cost preview counts only the orgs the hunts running the rule read, cut to the orgs
    its author may read.

    A production+team write is routed to a review branch instead of the branch the
    runner git-syncs, and the ``X-DFE-Review-Required`` header says so.
    """
    from dfe_engine.hunts.rule_creation_service import RuleCreateRequest as SvcRequest

    if registry.name_exists(body.name):
        raise ProblemError(409, f"Rule '{body.name}' already exists", code="conflict")

    service = _rule_service(settings, sources)
    display = (
        body.display_name if body.display_name is not None else default_display_name(body.name)
    )

    svc_request = SvcRequest(
        name=display,
        severity=body.severity,
        where_clause=body.where_clause,
        condition=body.condition,
        cel_filter=body.cel_filter,
        hunt_name=body.hunt_name,
        source=body.source,
        estimate_cost=body.estimate_cost,
        cost_window_minutes=body.cost_window_minutes,
    )

    fence = _preview_tenant_ids(request, user, hunts, body.name, body.hunt_name)
    result = service.create_rule(svc_request, body.name, tenant_ids=fence)
    _refuse_uncompilable(result)
    _refuse_matches_everything(result)
    outcome = registry.save(result.rule, created_by=git_author(user))
    apply_review_headers(response, outcome)
    audit_resource_change(user.user_id, "rule", body.name, "created", review_audit_detail(outcome))
    return _build_create_response(result)


@router.post(
    "/import",
    summary="Import a SELECT as a Rule's Fields",
    response_model=RuleImportResponse,
    responses=_IMPORT_REFUSED_RESPONSES,
    dependencies=[Depends(require_action(scopes_dict["rule_validate"]))],
)
def import_rule(
    body: RuleImportRequest, user: CurrentUser, settings: Settings, sources: SourceReg
) -> RuleImportResponse:
    """Read a SELECT into the source and WHERE clause a rule is saved with, saving nothing.

    The FROM becomes ``source`` when its table is a registered source. Otherwise
    ``source`` is null and the caller picks one. The column list, ORDER BY, LIMIT, SETTINGS,
    a ``{timestamp_condition}`` placeholder and time bounds on timestamp columns are dropped
    and listed in ``dropped``. GROUP BY, HAVING, a JOIN, a UNION, a WITH clause and a
    subquery in FROM are refused with 422 ``import_refused``.
    """
    return _import_response(_read_select(body.sql, settings, sources))


@router.post(
    "/from-hyperdx",
    summary="Read a Search View as a Rule's Fields",
    response_model=RuleFromHyperdxResponse,
    responses=_IMPORT_REFUSED_RESPONSES,
    dependencies=[Depends(require_action(scopes_dict["rule_validate"]))],
)
def rule_from_hyperdx(
    body: RuleFromHyperdxRequest,
    user: CurrentUser,
    settings: Settings,
    registry: RuleReg,
    sources: SourceReg,
) -> RuleFromHyperdxResponse:
    """Read the SQL a Search view renders to into the fields a rule editor opens with.

    The dfe-hyperdx sanitiser strips the view's own meta (time bounds, LIMIT,
    ``__hdx_time_bucket``, SETTINGS), then the result is read as ``POST /rules/import``
    reads a SELECT. Nothing is saved, so a rule never lands without a source. A free
    rule name is drawn from the saved-search name. RBAC: ``rule:validate``.
    """
    from dfe_engine.hunts.hdx_sanitizer import HdxSanitizeError, HdxSanitizer

    try:
        cleaned = HdxSanitizer().sanitize(body.raw_sql)
    except HdxSanitizeError as exc:
        raise ProblemError(422, str(exc), code=ErrorCode.IMPORT_REFUSED) from exc
    imported = _read_select(cleaned.clean_sql, settings, sources)
    fields = _import_response(imported).model_dump()
    if body.source:
        _prefer_named_source(fields, body.source, source_check(sources))
    rule_id = _unique_rule_id(registry, body.saved_search_name)
    return RuleFromHyperdxResponse(
        **fields,
        name=rule_id,
        display_name=body.saved_search_name or default_display_name(rule_id),
        severity=body.severity,
        hunt_name=body.hunt_name,
        sanitize_summary=sanitize_summary(cleaned),
    )


@router.post(
    "/check",
    response_model=RuleCheckResponse,
    dependencies=[Depends(require_action(scopes_dict["rule_validate"]))],
)
def check_rule(
    body: RuleCheckRequest,
    user: CurrentUser,
    settings: Settings,
    sources: SourceReg,
    request: Request,
    hunts: OptionalHuntConfigReg,
) -> RuleCheckResponse:
    """Check a source and WHERE clause as a save would, writing nothing.

    Answers 200 with ``valid`` false and the reasons in ``errors`` for a rule a save
    would refuse, so an editor can show them as the author types. ``warnings`` carries
    what a save accepts but the author should read. The alert-volume preview does not run,
    which ``POST /rules/preview`` is for.
    """
    from dfe_engine.hunts.rule_creation_service import RuleCreateRequest as SvcRequest

    svc_request = SvcRequest(
        name="check", source=body.source, where_clause=body.where_clause, measure_volume=False
    )
    fence = _preview_tenant_ids(request, user, hunts, "", None)
    result = _rule_service(settings, sources).create_rule(svc_request, "check", tenant_ids=fence)
    errors = _save_blockers(result)
    if result.matches_everything:
        errors.append(SqlValidationError(message=result.matches_everything))
    return RuleCheckResponse(
        valid=not errors,
        errors=errors,
        warnings=[*result.rule.warnings, *result.json_path_warnings],
    )


@router.post(
    "/preview",
    response_model=RulePreviewResponse,
    dependencies=[Depends(require_action(scopes_dict["rule_validate"]))],
)
def preview_rule(
    body: RulePreviewRequest,
    user: CurrentUser,
    settings: Settings,
    registry: RuleReg,
    sources: SourceReg,
    request: Request,
    hunts: OptionalHuntConfigReg,
) -> RulePreviewResponse:
    """Compile a condition and check it exactly as a save would, writing nothing.

    Runs the creation pipeline of ``POST /rules`` (compile, parse, the forbidden-keyword
    and column checks, the matches-everything check and the alert-volume preview) under the
    same org fence, and stops before the save. A condition the save would refuse comes back
    with 200 and its reasons in ``sql_errors`` or ``matches_everything``, so a builder can
    show them as the author edits. A body that is not a condition at all is 422.
    """
    from dfe_engine.hunts.rule_creation_service import RuleCreateRequest as SvcRequest

    rule_id = body.name or "preview"
    hunt_name = body.hunt_name
    if hunt_name is None and body.name:
        # An update keeps the stored rule's hunt when the body names none, so the preview does.
        try:
            hunt_name = registry.get(body.name).hunt_name
        except RuleNotFoundError:
            pass
    svc_request = SvcRequest(
        name=rule_id,
        source=body.source,
        condition=body.condition,
        cel_filter=body.cel_filter,
        hunt_name=hunt_name,
        cost_window_minutes=body.cost_window_minutes,
    )
    fence = _preview_tenant_ids(request, user, hunts, body.name or "", hunt_name)
    result = _rule_service(settings, sources).create_rule(svc_request, rule_id, tenant_ids=fence)
    return RulePreviewResponse(
        user_sql=result.rule.original_sql,
        source_db=result.rule.source_db,
        source_table=result.rule.source_table,
        where_clause=result.rule.where_clause,
        sql_errors=_save_blockers(result),
        matches_everything=result.matches_everything,
        warnings=result.json_path_warnings,
        cost_estimate=result.cost_estimate,
    )


@router.get(
    "/{name}",
    response_model=RuleResponse,
    dependencies=[Depends(require_action(scopes_dict["rule_read"]))],
)
def get_rule(
    name: str, user: CurrentUser, registry: RuleReg, settings: Settings, sources: OptionalSourceReg
):
    """Get a detection rule by file name.

    A stored rule with no source whose table is a registered source's label is returned
    under that source.
    """
    try:
        rule = registry.get(name)
    except RuleNotFoundError:
        raise ProblemError(404, f"Rule '{name}' not found", code="not_found") from None
    sql_errors = _sql_errors_for_original_sql(settings, rule.original_sql)
    return _rule_to_response(
        rule, sql_errors=sql_errors, source=_resolved(rule.source, rule.source_table, sources)
    )


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
    sources: SourceReg,
    request: Request,
    response: Response,
    hunts: OptionalHuntConfigReg,
):
    """Replace a detection rule (re-runs creation pipeline, preserves created_at).

    The replacement is written as a ``where_clause`` or a ``condition`` on a registered
    ``source``, as on create, and a rule replaced with a WHERE no longer carries a condition.

    A replacement the hunt runner could not compile is refused with 422 and the
    stored rule is left as it was. A production+team write is routed to a review
    branch instead of the branch the runner git-syncs, and the
    ``X-DFE-Review-Required`` header says so.
    """
    try:
        existing = registry.get(name)
    except RuleNotFoundError:
        raise ProblemError(404, f"Rule '{name}' not found", code="not_found") from None

    from dfe_engine.hunts.rule_creation_service import RuleCreateRequest as SvcRequest

    service = _rule_service(settings, sources)
    effective_display = body.display_name if body.display_name is not None else existing.name
    effective_hunt = body.hunt_name if body.hunt_name is not None else existing.hunt_name
    svc_request = SvcRequest(
        name=effective_display,
        severity=body.severity,
        where_clause=body.where_clause,
        condition=body.condition,
        cel_filter=body.cel_filter,
        hunt_name=effective_hunt,
        source=body.source,
        estimate_cost=body.estimate_cost,
        cost_window_minutes=body.cost_window_minutes,
    )
    fence = _preview_tenant_ids(request, user, hunts, name, effective_hunt)
    result = service.create_rule(svc_request, name, tenant_ids=fence)
    _refuse_uncompilable(result)
    _refuse_matches_everything(result)
    updated = result.rule.model_copy(
        update={"created_at": existing.created_at, "name": effective_display},
    )
    outcome = registry.save(updated, created_by=git_author(user))
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
    compiling the rule until that branch is merged, which ``X-DFE-Review-Required`` says.
    """
    used_by = hunt_registry.hunt_names_referencing_rule(name)
    if used_by:
        hunts = ", ".join(sorted(used_by))
        raise ProblemError(409, f"Rule '{name}' is referenced by hunt(s): {hunts}", code="conflict")
    try:
        outcome = registry.delete(name, created_by=git_author(user))
    except RuleNotFoundError:
        raise ProblemError(404, f"Rule '{name}' not found", code="not_found") from None
    apply_review_headers(response, outcome)
    audit_resource_change(user.user_id, "rule", name, "deleted", review_audit_detail(outcome))


# -- Helpers --------------------------------------------------


def _rule_service(settings: Settings, sources: Any = None):
    """The creation service every rule check and save runs through.

    Args:
        settings: The engine settings.
        sources: The source registry. With it a rule naming a label it does not carry is refused.
    """
    return service_from_settings(
        settings, is_source=source_check(sources) if sources is not None else None
    )


def _resolved(source: str | None, source_table: str | None, sources: Any) -> str | None:
    """The registered source a stored rule reads, else the source it stored."""
    if sources is None:
        return source
    return resolved_source(source, source_table, source_check(sources)) or source


def _with_resolved_sources(rows: list[dict], sources: Any) -> list[dict]:
    """The registry's rows, each under the registered source it reads."""
    return [
        {**row, "source": _resolved(row.get("source"), row.get("source_table"), sources)}
        for row in rows
    ]


def _read_select(sql: str, settings: Settings, sources: Any) -> RuleImport:
    """Read a SELECT into a rule's fields, or answer 422 ``import_refused`` with the reason."""
    try:
        return import_select(
            sql,
            is_source=source_check(sources),
            data_database=settings.clickhouse.effective_data_database,
        )
    except ImportRefusedError as exc:
        raise ProblemError(422, str(exc), code=ErrorCode.IMPORT_REFUSED) from exc


def _dropped(part: DroppedPart) -> DroppedPartResponse:
    return DroppedPartResponse(kind=part.kind, text=part.text)


def _import_response(imported: RuleImport) -> RuleImportResponse:
    return RuleImportResponse(
        source=imported.source,
        needs_source=imported.source is None,
        from_table=imported.from_table,
        where_clause=imported.where_clause,
        dropped=[_dropped(part) for part in imported.dropped],
        warnings=imported.warnings,
    )


def _prefer_named_source(
    fields: dict[str, Any], named: str, is_source: Callable[[str], bool]
) -> None:
    """Take the source the Search view named over the FROM's, when it is registered."""
    if is_source(named):
        fields["source"] = named
        fields["needs_source"] = False
    else:
        fields["warnings"] = [
            *fields["warnings"],
            f"The view's source {named!r} is not registered.",
        ]


def _preview_tenant_ids(
    request: Request, user: CurrentUser, hunts: Any, rule: str, hunt_name: str | None
) -> list[str] | None:
    """The orgs a rule write's volume preview counts, or None for every org.

    A rule a hunt runs, or names as its hunt, is counted over those hunts' customers,
    as the compiled statements read them. Every rule is held to the orgs its author
    may read as well, so a count never reports another org's rows.

    Args:
        request: The request, for the role definitions and the org registry.
        user: The rule's author.
        hunts: The hunt registry, or None where the deployment carries none.
        rule: The rule's name.
        hunt_name: The hunt the rule names as its own, if any.

    Returns:
        The tenant ids, sorted, or None when the author reads every org and no hunt
        runs the rule.
    """
    readable = held_tenant_ids(request, user, scopes_dict["rule_write"])
    running = set(hunts.hunt_names_referencing_rule(rule)) if hunts is not None else set()
    if hunt_name:
        running.add(hunt_name)
    hunt_ids: set[str] | None = None
    for hunt in sorted(running):
        try:
            definition = hunts.get(hunt) if hunts is not None else None
        except HuntConfigNotFoundError:
            continue
        if definition is not None:
            hunt_ids = (hunt_ids or set()) | set(hunt_tenant_ids(definition)[0])
    if hunt_ids is None:
        return readable
    if readable is None:
        return sorted(hunt_ids)
    return sorted(hunt_ids & set(readable))


def _sql_errors_for_original_sql(settings: Settings, original_sql: str) -> list[SqlValidationError]:
    if not original_sql.strip():
        return []
    service = _rule_service(settings)
    return _map_sql_errors(service.validate_sql(original_sql))


def _rule_to_response(
    rule,
    *,
    sql_errors: list[SqlValidationError] | None = None,
    volume_warnings: list[str] | None = None,
    source: str | None = None,
) -> RuleResponse:
    """A rule as the API returns it, with the create-time readings beside its stored warnings.

    ``source`` overrides the stored one, which a rule saved before sources were registered
    takes from its table.
    """
    return RuleResponse(
        name=rule.rule_id,
        display_name=rule.name,
        severity=rule.severity,
        source_db=rule.source_db,
        source_table=rule.source_table,
        where_clause=rule.where_clause,
        cel_filter=rule.cel_filter,
        condition=rule.condition,
        original_sql=rule.original_sql,
        hunt_name=rule.hunt_name,
        source=source if source is not None else rule.source,
        warnings=[*rule.warnings, *(volume_warnings or [])],
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


def _save_blockers(result) -> list[SqlValidationError]:
    """Why a save of the checked rule is refused as invalid, empty when it is not.

    SQL that does not validate is refused on its SQL errors. SQL that validates
    can still leave nothing to run, such as a search with no filter, which the
    rule's own execution check reports.
    """
    sql_errors = _map_sql_errors(result.sql_errors or [])
    if not sql_errors:
        sql_errors = [SqlValidationError(message=m) for m in result.rule.validate_rule()]
    return sql_errors


def _refuse_uncompilable(result) -> None:
    """Refuse, before anything is written, a rule the hunt runner cannot compile.

    Raises:
        ProblemError: 422 ``invalid_sql``, every error in ``sql_errors``.
    """
    sql_errors = _save_blockers(result)
    if not sql_errors:
        return
    raise ProblemError(
        422,
        "Rule not saved: " + "; ".join(e.message for e in sql_errors),
        code=ErrorCode.INVALID_SQL,
        sql_errors=[e.model_dump(exclude_none=True) for e in sql_errors],
    )


def _refuse_matches_everything(result) -> None:
    """Refuse, before anything is written, a rule whose condition matches every event.

    Raises:
        ProblemError: 422 ``rule_matches_everything``, with the source and the
            detection WHERE in context.
    """
    if not result.matches_everything:
        return
    raise ProblemError(
        422,
        result.matches_everything,
        code=ErrorCode.RULE_MATCHES_EVERYTHING,
        source=source_label(result.rule),
        where_clause=result.rule.where_clause,
    )


def _slugify_rule_id(text: str | None) -> str:
    """Slug free text into a rule name (``DETECTION_NAME``), falling back to a default."""
    base = re.sub(r"[^a-zA-Z0-9_-]+", "-", (text or "").strip()).strip("-").lower()
    return base[:MAX_NAME_LENGTH] or "hyperdx-rule"


def _unique_rule_id(registry, saved_search_name: str | None) -> str:
    """Derive a rule id from the saved-search name, de-duplicated against the registry.

    The base is cut short to fit each ``-N`` suffix, so every candidate is a name a
    hunt can reference.
    """
    base = _slugify_rule_id(saved_search_name)
    candidate = base
    n = 2
    while registry.name_exists(candidate):
        suffix = f"-{n}"
        candidate = f"{base[: MAX_NAME_LENGTH - len(suffix)]}{suffix}"
        n += 1
    return candidate


def _build_create_response(result) -> RuleCreateResponse:
    sql_errors = _map_sql_errors(result.sql_errors or [])
    return RuleCreateResponse(
        rule=_rule_to_response(
            result.rule,
            sql_errors=sql_errors,
            volume_warnings=[*result.volume_warnings, *result.json_path_warnings],
        ),
        sql_errors=sql_errors,
        cost_estimate=result.cost_estimate,
    )
