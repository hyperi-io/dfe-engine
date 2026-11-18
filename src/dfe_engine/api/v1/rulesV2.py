# ruff: noqa: N999
"""Rules router -- a rule is a CEL filter or a WHERE fragment, shared by hunts.

GET    /api/v1/rules-v2              -> Paginated list (search)
POST   /api/v1/rules-v2              -> Create a rule from a CEL filter or a WHERE fragment
GET    /api/v1/rules-v2/{name}    -> Rule detail
PUT    /api/v1/rules-v2/{name}    -> Update rule
DELETE /api/v1/rules-v2/{name}    -> Delete rule
POST   /api/v1/rules-v2/validate     -> Validate SQL without creating
POST   /api/v1/rules-v2/preview      -> Compile a condition and check it as a save would, saving nothing

A rule names no source and no hunt. The hunt that runs it names the source table
and the rules it runs. Create and update take either ``cel_filter`` or
``where_clause``, and the stored rule keeps that detection without a full SQL query.
"""

import functools
import re
from typing import Annotated, Any

import sqlglot
from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, BeforeValidator, Field, model_validator
from scalo.api import ListQuery, Page, ProblemError, list_query, paginate, sort_by_order
from scalo.concurrency import run_blocking
from scalo.expression import ExpressionError, TranspileError
from sqlglot import exp
from sqlglot.errors import ParseError, SqlglotError

from dfe_engine.api.conventions import apply_search
from dfe_engine.api.deps import (
    CurrentUser,
    HuntConfigReg,
    OptionalHuntConfigReg,
    RuleReg,
    Settings,
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
from dfe_engine.hunts.rule_creation_service import CostEstimate
from dfe_engine.hunts.rule_guard import (
    match_everything,
    refuse_condition_escape,
    refuse_offbox_calls,
    source_label,
)
from dfe_engine.hunts.rule_model import Rule, Severity
from dfe_engine.hunts.rule_model import RuleCreate as StoredRuleCreate
from dfe_engine.hunts.rule_registry import RuleNotFoundError
from dfe_engine.names import MAX_NAME_LENGTH, RuleName

router = APIRouter(prefix="/rules-v2", tags=["Rules V2"], dependencies=[WRITE_TURN])

_REFUSED_RULE_RESPONSES: dict[int | str, dict] = {
    422: {
        "description": (
            "Refused, nothing written. The body failed validation (code validation_error): "
            "give a CEL filter or a WHERE fragment, one of the two, and not a full SQL "
            "statement. Or the detection was refused -- the WHERE fragment does not parse, "
            "calls a function that reads outside the row (url, s3, a dictionary) or sends "
            "it to another service (the ai* functions), reads another table, or the CEL "
            "filter does not transpile (code invalid_sql, the errors in sql_errors). "
            "Or the rule matches every event (code rule_matches_everything, the detection "
            "WHERE in context)."
        ),
    },
}

# A full statement is not a detection condition. The hunt supplies FROM.
_STATEMENT = re.compile(
    r"(?is)^(select|with|insert|update|delete|create|drop|alter|truncate|grant|revoke|merge)\b"
)
_FORBIDDEN_KEYWORDS = re.compile(
    r"\b(?:INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE|MERGE)\b",
    re.IGNORECASE,
)
_WHERE_FIX = "Pass the condition only, for example severity = 'high'."


def _normalize_where(sql: str) -> str:
    """The detection condition, without a leading WHERE keyword."""
    return re.sub(r"(?is)^where\b", "", sql.strip()).strip()


_CEL_FIX = (
    "Check the expression's syntax and that it calls only functions the hunt profile "
    "allows. To divide two fields, wrap an operand in double() or int()."
)


def _empty_text(value: object) -> object:
    """Strip text, and treat a blank string as absent."""
    if not isinstance(value, str):
        return value
    text = value.strip()
    return text or None


Description = Annotated[str | None, BeforeValidator(_empty_text)]
"""An optional string a blank value does not set."""


# -- Request/response models -----------------------------------


class _RuleWriteFields(BaseModel):
    """Fields a create or update sets. A rule is a CEL filter or a WHERE fragment."""

    model_config = {"extra": "forbid"}

    display_name: str | None = Field(
        default=None,
        description="Human-readable label (defaults from ``name`` when omitted on create)",
    )
    description: Description = Field(default=None, description="What the rule detects")
    severity: Severity = Field(default="medium", description="low|medium|high|critical")
    where_clause: Description = Field(
        default=None,
        min_length=1,
        description=(
            "Detection condition, the SQL after WHERE. The hunt that runs the rule names "
            "the table. Give this or ``cel_filter``, not both, and not a full SELECT"
        ),
    )
    cel_filter: Description = Field(
        default=None,
        min_length=1,
        description=(
            "CEL expression transpiled to the rule's WHERE. Give this or ``where_clause``, not both"
        ),
    )

    @model_validator(mode="after")
    def _cel_or_where(self) -> _RuleWriteFields:
        if (self.where_clause is None) == (self.cel_filter is None):
            raise ValueError("give 'where_clause' or 'cel_filter', one of the two")
        if self.where_clause is not None and _STATEMENT.match(_normalize_where(self.where_clause)):
            raise ValueError("where_clause is the detection condition, not a full SQL statement")
        return self


class RuleV2CreateRequest(_RuleWriteFields):
    """Create a detection rule from a CEL filter or a WHERE fragment."""

    name: RuleName = Field(description="Rule file name (YAML stem); must be unique")


class RuleV2UpdateRequest(_RuleWriteFields):
    """Replace a detection rule's label, description, severity, and detection."""


class SqlValidationRequest(BaseModel):
    """Body for validating a rule's SQL."""

    sql: str = Field(description="Full SELECT ... FROM ... query to validate")


class SqlValidationError(BaseModel):
    """One error in rule SQL, with where it is and a suggested fix."""

    message: str
    position: int | None = None
    suggestion: str | None = None


class SqlValidationResponse(BaseModel):
    """Whether rule SQL is valid, and its errors."""

    valid: bool
    errors: list[SqlValidationError] = Field(default_factory=list)


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
    cost_estimate: CostEstimate | None = None


class RuleV2Response(BaseModel):
    """A detection rule as the API returns it.

    ``where_clause`` is the condition a hunt runs. For a CEL rule it is the
    filter transpiled to SQL, and ``cel_filter`` is the expression the author wrote.
    """

    name: str = Field(description="Rule file name (YAML stem)")
    display_name: str = Field(description="Human-readable rule label")
    description: Description = Field(default=None, description="What the rule detects")
    severity: str
    where_clause: str = Field(description="Detection condition a hunt runs against its own source")
    cel_filter: str | None = Field(
        default=None,
        description="CEL expression the WHERE was transpiled from, absent when the rule is a WHERE fragment",
    )
    warnings: list[str] = Field(default_factory=list)
    sql_errors: list[SqlValidationError] = Field(default_factory=list)
    created_at: str


class RuleV2Summary(BaseModel):
    """A detection rule in a listing."""

    name: str = Field(description="Rule file name (YAML stem)")
    display_name: str
    description: Description = Field(default=None, description="What the rule detects")
    severity: str
    created_at: str


class RuleV2CreateResponse(BaseModel):
    """The created rule and any validation notes. Nothing here names a hunt or a source."""

    rule: RuleV2Response
    sanitize_summary: dict = Field(default_factory=dict)
    sql_errors: list[SqlValidationError] = Field(default_factory=list)


class RuleV2FromHyperdxRequest(BaseModel):
    """Create a hunt rule from the SQL a Search view runs.

    Search renders the view to ``raw_sql`` and sends it here. The create
    pipeline strips the UI meta (time bounds, LIMIT, ``__hdx_time_bucket``,
    SETTINGS) via the dfe-hyperdx sanitizer, and the engine derives a unique rule id
    from the saved-search name. The caller gets that id back and opens
    ``/rules-v2/{id}`` -- no id to invent, no IndexedDB round-trip.
    """

    model_config = {"extra": "forbid"}

    raw_sql: str = Field(
        min_length=1,
        description="The ClickHouse SELECT the Search view renders to",
    )
    saved_search_name: str | None = Field(
        default=None, description="Search saved-search name; seeds the rule id and label"
    )
    severity: Severity = Field(default="medium", description="low|medium|high|critical")
    description: Description = Field(default=None, description="What the rule detects")


class RuleV2FromHyperdxResponse(BaseModel):
    """The rule created from a Search query."""

    id: str = Field(description="Created rule id (YAML stem); open at /rules-v2/{id}")
    display_name: str
    sanitize_summary: dict = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    sql_errors: list[SqlValidationError] = Field(default_factory=list)


# -- Endpoints ------------------------------------------------


RulesList = Annotated[
    ListQuery,
    Depends(
        list_query(
            order_by=("name", "display_name", "description", "severity", "created_at"),
            tiebreak="name",
        )
    ),
]


@router.get(
    "",
    response_model=Page[RuleV2Summary],
    dependencies=[Depends(require_action(scopes_dict["rule_read"]))],
)
def list_rules(
    user: CurrentUser,
    registry: RuleReg,
    query: RulesList,
    search: str | None = Query(
        None,
        description="Case-insensitive search in name, display_name, description, severity",
    ),
    severity: str | None = Query(None, description="Filter by severity"),
):
    """List detection rules with pagination and search."""
    raw = registry.list_rules()

    if severity is not None:
        sev = severity.lower()
        raw = [row for row in raw if row.get("severity", "").lower() == sev]

    # A missing description is empty for search, and the summary shows it as absent.
    for row in raw:
        row["description"] = row.get("description") or ""

    raw = apply_search(raw, search, ["name", "display_name", "description", "severity"])
    raw = sort_by_order(raw, query.order_by)

    summaries = [RuleV2Summary.model_validate(row) for row in raw]
    return paginate(summaries, query, bound={"search": search, "severity": severity})


@router.post(
    "",
    response_model=RuleV2CreateResponse,
    status_code=201,
    responses=_REFUSED_RULE_RESPONSES,
    dependencies=[Depends(require_action(scopes_dict["rule_write"]))],
)
def create_rule(
    body: RuleV2CreateRequest,
    user: CurrentUser,
    settings: Settings,
    registry: RuleReg,
    response: Response,
):
    """Create a detection rule from a CEL filter or a WHERE fragment.

    The rule stores that detection and nothing about a source or a hunt. A hunt
    names the rules it runs and the table they read. A filter that does not
    transpile, or a WHERE fragment the hunt runner would refuse, is 422 and
    nothing is written.

    A production+team write is routed to a review branch instead of the branch the
    runner git-syncs, and the ``X-DFE-Review-Required`` header says so.
    """
    if registry.name_exists(body.name):
        raise ProblemError(409, f"Rule '{body.name}' already exists", code="conflict")

    display = (
        body.display_name if body.display_name is not None else default_display_name(body.name)
    )
    rule, sql_errors = _rule_from_write(
        settings, body, rule_id=body.name, display_name=display, description=body.description
    )
    _refuse_detection(rule, sql_errors)
    outcome = registry.save(rule, created_by=git_author(user))
    apply_review_headers(response, outcome)
    audit_resource_change(user.user_id, "rule", body.name, "created", review_audit_detail(outcome))
    return _build_create_response(rule, sql_errors)


@router.post(
    "/from-hyperdx",
    summary="Create Rule From Search",
    response_model=RuleV2FromHyperdxResponse,
    status_code=201,
    responses=_REFUSED_RULE_RESPONSES,
    dependencies=[Depends(require_action(scopes_dict["rule_write"]))],
)
async def create_rule_from_hyperdx(
    body: RuleV2FromHyperdxRequest,
    user: CurrentUser,
    settings: Settings,
    registry: RuleReg,
    request: Request,
    response: Response,
    hunts: OptionalHuntConfigReg,
):
    """Create a rule from the SQL a Search view renders to.

    The pipeline keeps the detection WHERE and drops the query's FROM, so the
    stored rule names no source and no hunt. A view that does not compile, or
    that matches every event, is refused with 422. RBAC: ``rule:write``
    (data_analyst) -- ``org_viewer`` has neither this grant nor the UI button.
    """
    return await run_blocking(
        functools.partial(
            _create_rule_from_sql, body, user, settings, registry, request, response, hunts
        )
    )


def _create_rule_from_sql(
    body: RuleV2FromHyperdxRequest,
    user: CurrentUser,
    settings: Settings,
    registry: RuleReg,
    request: Request,
    response: Response,
    hunts: OptionalHuntConfigReg,
) -> RuleV2FromHyperdxResponse:
    """The deploy-repo and ClickHouse half of ``create_rule_from_hyperdx``, on a worker thread."""
    from dfe_engine.hunts.rule_creation_service import RuleV2CreateRequest as SvcRequest

    rule_id = _unique_rule_id(registry, body.saved_search_name)
    display = body.saved_search_name or default_display_name(rule_id)

    service = _rule_service(settings)
    svc_request = SvcRequest(
        name=display,
        severity=body.severity,
        source_type="hyperdx",
        user_sql=body.raw_sql,
    )

    fence = _preview_tenant_ids(request, user, hunts, rule_id, None)
    result = service.create_rule(svc_request, rule_id, tenant_ids=fence)
    _refuse_uncompilable(result)
    _refuse_matches_everything(result)
    # Search SQL names a table. The rule keeps the WHERE; the hunt names the source.
    stored = _without_hunt_or_source(result.rule, description=body.description)
    outcome = registry.save(stored, created_by=git_author(user))
    apply_review_headers(response, outcome)
    audit_resource_change(user.user_id, "rule", rule_id, "created", review_audit_detail(outcome))
    return RuleV2FromHyperdxResponse(
        id=rule_id,
        display_name=result.rule.name,
        sanitize_summary=result.sanitize_summary or {},
        warnings=[*result.rule.warnings, *result.volume_warnings],
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
    service = _rule_service(settings)
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
    request: Request,
    hunts: OptionalHuntConfigReg,
) -> RulePreviewResponse:
    """Compile a condition and check it exactly as a save would, writing nothing.

    Runs the creation pipeline of ``POST /rules-v2`` (compile, parse, the forbidden-keyword
    and column checks, the matches-everything check and the alert-volume preview) under the
    same org fence, and stops before the save. A condition the save would refuse comes back
    with 200 and its reasons in ``sql_errors`` or ``matches_everything``, so a builder can
    show them as the author edits. A body that is not a condition at all is 422.
    """
    from dfe_engine.hunts.rule_creation_service import RuleV2CreateRequest as SvcRequest

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
    result = _rule_service(settings).create_rule(svc_request, rule_id, tenant_ids=fence)
    return RulePreviewResponse(
        user_sql=result.rule.original_sql,
        source_db=result.rule.source_db,
        source_table=result.rule.source_table,
        where_clause=result.rule.where_clause,
        sql_errors=_save_blockers(result),
        matches_everything=result.matches_everything,
        cost_estimate=result.cost_estimate,
    )


@router.get(
    "/{name}",
    response_model=RuleV2Response,
    dependencies=[Depends(require_action(scopes_dict["rule_read"]))],
)
def get_rule(name: str, user: CurrentUser, registry: RuleReg, settings: Settings):
    """Get a detection rule by file name."""
    try:
        rule = registry.get(name)
    except RuleNotFoundError:
        raise ProblemError(404, f"Rule '{name}' not found", code="not_found") from None
    sql_errors = _where_errors(settings, rule.where_clause)
    return _rule_to_response(rule, sql_errors=sql_errors)


@router.put(
    "/{name}",
    response_model=RuleV2CreateResponse,
    responses=_REFUSED_RULE_RESPONSES,
    dependencies=[Depends(require_action(scopes_dict["rule_write"]))],
)
def update_rule(
    name: str,
    body: RuleV2UpdateRequest,
    user: CurrentUser,
    settings: Settings,
    registry: RuleReg,
    response: Response,
):
    """Replace a detection rule's label, description, severity, and detection.

    The replacement is a CEL filter or a WHERE fragment, as on create. It does
    not record a source or a hunt. A replacement the hunt runner would refuse is
    422 and the stored rule is left as it was.

    A production+team write is routed to a review branch instead of the branch the
    runner git-syncs, and the ``X-DFE-Review-Required`` header says so.
    """
    try:
        existing = registry.get(name)
    except RuleNotFoundError:
        raise ProblemError(404, f"Rule '{name}' not found", code="not_found") from None

    display = body.display_name if body.display_name is not None else existing.name
    # Omitted keeps the stored description. An empty string clears it.
    if "description" in body.model_fields_set:
        description = body.description
    else:
        description = existing.description
    rule, sql_errors = _rule_from_write(
        settings, body, rule_id=name, display_name=display, description=description
    )
    _refuse_detection(rule, sql_errors)
    updated = rule.model_copy(update={"created_at": existing.created_at})
    outcome = registry.save(updated, created_by=git_author(user))
    apply_review_headers(response, outcome)
    audit_resource_change(user.user_id, "rule", name, "updated", review_audit_detail(outcome))
    return _build_create_response(updated, sql_errors)


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


def _rule_service(settings: Settings):
    """The creation service every rule check and save runs through."""
    from dfe_engine.hunts.rule_creation_service import RuleCreationService
    from dfe_engine.settings import get_clickhouse_config

    return RuleCreationService(
        ch_config=get_clickhouse_config(settings),
        hunts=settings.hunts,
        data_database=settings.clickhouse.effective_data_database,
    )


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


def _rule_to_response(
    rule,
    *,
    sql_errors: list[SqlValidationError] | None = None,
) -> RuleV2Response:
    """A rule as the API returns it: the detection, and no source or hunt."""
    return RuleV2Response(
        name=rule.rule_id,
        display_name=rule.name,
        description=rule.description,
        severity=rule.severity,
        where_clause=rule.where_clause,
        cel_filter=rule.cel_filter,
        warnings=list(rule.warnings),
        sql_errors=sql_errors or [],
        created_at=rule.created_at,
    )


def _without_hunt_or_source(rule: Rule, *, description: str | None) -> Rule:
    """A copy that keeps the detection and drops source, hunt, and the original query."""
    return rule.model_copy(
        update={
            "description": description,
            "source_db": None,
            "source_table": None,
            "source": None,
            "hunt_name": None,
            "original_sql": "",
            "condition": None,
            "had_select_star": False,
        }
    )


def _rule_from_write(
    settings: Settings,
    body: _RuleWriteFields,
    *,
    rule_id: str,
    display_name: str,
    description: str | None,
) -> tuple[Rule, list[SqlValidationError]]:
    """The rule a create or update would store, and why it would be refused."""
    if body.cel_filter is not None:
        try:
            rule = Rule.from_create(
                StoredRuleCreate(
                    name=display_name,
                    severity=body.severity,
                    cel_filter=body.cel_filter,
                ),
                rule_id=rule_id,
            )
        except (ExpressionError, TranspileError) as exc:
            rule = Rule(
                rule_id=rule_id,
                name=display_name,
                severity=body.severity,
                cel_filter=body.cel_filter,
            )
            return _without_hunt_or_source(rule, description=description), [
                SqlValidationError(message=f"CEL filter refused: {exc}", suggestion=_CEL_FIX)
            ]
        errors = _where_errors(settings, rule.where_clause)
        return _without_hunt_or_source(rule, description=description), errors

    where = _normalize_where(body.where_clause or "")
    errors = _where_errors(settings, where)
    rule = Rule(
        rule_id=rule_id,
        name=display_name,
        description=description,
        severity=body.severity,
        where_clause=where,
    )
    return rule, errors


def _where_errors(settings: Settings, where: str) -> list[SqlValidationError]:
    """Why a WHERE fragment cannot be saved, empty when a hunt can run it."""
    text = where.strip()
    if not text:
        return [
            SqlValidationError(
                message="Rule has empty detection logic (WHERE clause).",
                suggestion=_WHERE_FIX,
            )
        ]
    if _STATEMENT.match(text):
        return [
            SqlValidationError(
                message="A rule takes a WHERE fragment or a CEL filter, not a full SQL statement.",
                suggestion=_WHERE_FIX,
            )
        ]
    try:
        parsed = sqlglot.parse_one(text, read="clickhouse")
    except (ParseError, SqlglotError) as exc:
        return [
            SqlValidationError(
                message=f"WHERE clause does not parse as ClickHouse: {exc}",
                suggestion=_WHERE_FIX,
            )
        ]
    if isinstance(parsed, exp.Query):
        return [
            SqlValidationError(
                message="A rule takes a WHERE fragment or a CEL filter, not a full SQL statement.",
                suggestion=_WHERE_FIX,
            )
        ]

    from dfe_engine.hunts.rule_creation_service import _mask_quoted

    errors: list[SqlValidationError] = []
    keyword = _FORBIDDEN_KEYWORDS.search(_mask_quoted(text))
    if keyword is not None:
        errors.append(
            SqlValidationError(
                message=f"WHERE contains forbidden keyword '{keyword.group(0).upper()}'.",
                position=keyword.start(),
                suggestion="A detection condition cannot change data.",
            )
        )
    offbox = refuse_offbox_calls(text)
    if offbox is not None:
        errors.append(
            SqlValidationError(
                message=offbox,
                suggestion=(
                    "A rule matches rows of the hunt's source table; take the call out of "
                    "the condition."
                ),
            )
        )
    escape = refuse_condition_escape(text, lookup_databases=settings.hunts.lookup_databases)
    if escape is not None:
        errors.append(
            SqlValidationError(
                message=escape,
                suggestion=(
                    "A rule matches rows of the hunt's source table; compare against a list of "
                    "values, not another table."
                ),
            )
        )
    return errors


def _refuse_detection(rule: Rule, sql_errors: list[SqlValidationError]) -> None:
    """Refuse, before anything is written, a detection a hunt cannot run.

    Raises:
        ProblemError: 422 ``invalid_sql`` or ``rule_matches_everything``.
    """
    if sql_errors:
        raise ProblemError(
            422,
            "Rule not saved: " + "; ".join(error.message for error in sql_errors),
            code=ErrorCode.INVALID_SQL,
            sql_errors=[error.model_dump(exclude_none=True) for error in sql_errors],
        )
    reason = match_everything(rule.where_clause, "the hunt's source")
    if reason:
        raise ProblemError(
            422,
            reason,
            code=ErrorCode.RULE_MATCHES_EVERYTHING,
            where_clause=rule.where_clause,
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


def _build_create_response(
    rule: Rule, sql_errors: list[SqlValidationError]
) -> RuleV2CreateResponse:
    return RuleV2CreateResponse(
        rule=_rule_to_response(rule, sql_errors=sql_errors),
        sql_errors=sql_errors,
    )
