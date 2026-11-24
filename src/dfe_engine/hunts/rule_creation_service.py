"""Rule Creation Service -- HyperDX-aware rule creation pipeline.

Orchestrates the full rule creation workflow:
1. HyperDX SQL sanitization (if source_type="hyperdx")
2. SQL syntax validation with helpful error messages
3. Rule creation via Rule.from_create()
4. The match-everything check, which the API refuses on
5. The alert-volume preview: a count over a lookback window on the rule's
   source table, banded into warnings on the rule
6. AI analysis stub for async TS runner consumption

dfe-engine is a library -- no HTTP endpoints here. The control-plane
wraps this service in API routes.

Usage::

    from dfe_engine.hunts.rule_creation_service import (
        RuleCreationService, RuleCreateRequest,
    )

    service = RuleCreationService()
    result = service.create_rule(
        RuleCreateRequest(
            name="Certutil Abuse",
            severity="high",
            source="windows-audit",
            where_clause="process_name = 'certutil.exe'",
        ),
        rule_id="win_cert_01",
    )
    # result.rule -- Rule instance
    # result.sql_errors -- validation issues
    # result.sanitize_summary -- what was stripped
"""

import re
import time
from collections.abc import Callable
from typing import Any, Literal, Protocol

import sqlglot
from clickhouse_connect.driver.exceptions import ClickHouseError
from pydantic import BaseModel, Field, model_validator
from scalo.expression import ExpressionError, TranspileError
from scalo.logger import logger
from scalo.resilience import ServiceUnavailable
from sqlglot.errors import ParseError

from ..clickhouse.errors import ErrorCategory, parse_code, wrap_ch_error
from ..clickhouse.quoting import plain_source_name, quote_identifier
from ..schema.applier import SchemaApplyError, live_columns
from ..settings import DetectionGuardSettings, HuntsSettings
from .hdx_sanitizer import (
    REFUSED_CLAUSES,
    HdxSanitizeError,
    HdxSanitizer,
    split_time_window,
)
from .hunt_target_check import missing_columns
from .rule_condition import (
    MAX_CONDITION_SQL_BYTES,
    ORDERINGS,
    RuleCondition,
    RuleConditionError,
    compile_condition,
)
from .rule_guard import (
    VolumeBand,
    VolumeMeasure,
    match_everything,
    preview_settings,
    preview_sql,
    refuse_condition_escape,
    refuse_offbox_calls,
    source_label,
    unmeasured_warning,
    volume_verdict,
)
from .rule_import import find_placeholders
from .rule_import import sanitize_summary as summarise_sanitized
from .rule_json_paths import (
    referenced_json_paths,
    seen_paths_sql,
    unseen_paths,
    unseen_warning,
)
from .rule_model import Rule, RuleCreate
from .rule_rewriter import TIMESTAMP_COLUMNS, RuleRewriter, strip_time_placeholder
from .validator import rule_source

# -- Request / Response models ------------------------------


class RuleCreateRequest(BaseModel):
    """Input for the rule creation pipeline.

    The detection is written as ``user_sql``, a ``where_clause``, a structured
    ``condition`` or a ``cel_filter``. The first three are ways to write the same thing,
    so one of them. A ``where_clause`` or ``condition`` reads the table of ``source``,
    which must be registered, and compiles to the ``user_sql`` the pipeline checks.
    ``source_type`` controls whether HyperDX sanitization is applied to ``user_sql``.
    """

    name: str = Field(..., description="Human-readable rule name")
    severity: str = Field(default="medium", description="Detection severity")
    source_type: Literal["raw", "hyperdx"] = Field(
        default="raw",
        description="SQL origin: 'raw' for clean SQL, 'hyperdx' for HyperDX-generated",
    )
    user_sql: str | None = Field(default=None, description="User-supplied SQL SELECT")
    where_clause: str | None = Field(
        default=None, description="The rule's row filter, read from the table of ``source``"
    )
    condition: RuleCondition | None = Field(
        default=None, description="Structured condition, compiled to the rule's SQL"
    )
    cel_filter: str | None = Field(default=None, description="CEL expression for query pushdown")
    hunt_name: str | None = Field(default=None, description="Parent hunt name")
    source: str | None = Field(
        default=None, description="Source name for SourceRegistry resolution"
    )
    estimate_cost: bool = Field(
        default=True,
        description="Return the measured alert-volume preview in the result's cost_estimate",
    )
    measure_volume: bool = Field(
        default=True,
        description=(
            "Run the alert-volume preview at all. A check an author triggers as they type "
            "turns it off, since the preview counts a window of the source's rows"
        ),
    )
    cost_window_minutes: int | None = Field(
        default=None,
        description=(
            "Lookback window (minutes) the preview counts over. Absent or 0 uses the "
            "deployment's hunts.detection_guard.preview_window_minutes."
        ),
    )

    @model_validator(mode="after")
    def _require_sql_or_cel(self) -> RuleCreateRequest:
        written = [
            name
            for name, given in (
                ("user_sql", bool(self.user_sql)),
                ("where_clause", self.where_clause is not None),
                ("condition", self.condition is not None),
            )
            if given
        ]
        if not written and not self.cel_filter:
            raise ValueError(
                "At least one of 'user_sql', 'where_clause', 'condition' or 'cel_filter' "
                "must be provided."
            )
        if len(written) > 1:
            raise ValueError(f"Give one of {', '.join(repr(name) for name in written)}, not both.")
        if self.source_type != "raw" and (self.where_clause is not None or self.condition):
            raise ValueError(
                "The engine compiles a where_clause or condition, so its source_type is 'raw'."
            )
        return self


class SqlValidationError(BaseModel):
    """A SQL syntax or structure error."""

    message: str
    position: int | None = None
    suggestion: str | None = None


class CostEstimate(BaseModel):
    """What the alert-volume preview measured over its lookback window."""

    estimated_rows: int | None = Field(
        default=None, description="Events the rule matched in the window"
    )
    rows_in_window: int | None = Field(
        default=None, description="Events in the source table in the window"
    )
    projected_per_day: int | None = Field(
        default=None, description="Matches projected to a day at the window's rate"
    )
    match_ratio: float | None = Field(
        default=None, description="Share of the window's events the rule matched"
    )
    band: VolumeBand = Field(
        default=VolumeBand.UNMEASURED,
        description="ok, guidance, warn, plainly_bad, or unmeasured when the preview did not finish",
    )
    window_minutes: int
    duration_ms: float | None = Field(default=None, description="How long the preview took")
    warnings: list[str] = Field(default_factory=list)


class ChQueryClient(Protocol):
    """A ClickHouse client the parse check and the preview run through."""

    def query(self, query: str, *args: Any, **kwargs: Any) -> Any:
        """Run a SELECT and return a result carrying ``result_rows``."""
        ...

    def command(self, cmd: str, *args: Any, **kwargs: Any) -> Any:  # noqa: V107
        """Run a statement as sent, with no FORMAT clause appended."""
        ...


class AIAnalysisStub(BaseModel):
    """Context for async AI analysis -- consumed by TS runner, not processed here."""

    rule_id: str
    original_sql: str = ""
    clean_sql: str = ""
    where_clause: str = ""
    source_table: str | None = None
    source_db: str | None = None
    cost_estimate: CostEstimate | None = None


class RuleCreateResult(BaseModel):
    """Complete result of the rule creation pipeline."""

    rule: Rule
    sanitize_summary: dict[str, Any] = Field(default_factory=dict)
    sql_errors: list[SqlValidationError] = Field(default_factory=list)
    matches_everything: str | None = Field(
        default=None, description="Why the rule matches every event, when it does"
    )
    cost_estimate: CostEstimate | None = None
    volume_warnings: list[str] = Field(
        default_factory=list,
        description=(
            "What the alert-volume preview said about the rule just now. A point-in-time "
            "reading, so it is answered once and never stored with the rule"
        ),
    )
    json_path_warnings: list[str] = Field(
        default_factory=list,
        description=(
            "The _json paths the rule reads that the source's recent rows did not carry. "
            "A warning, never a refusal, since the data may simply not have arrived yet"
        ),
    )
    ai_context: AIAnalysisStub | None = None


# -- DDL/DML keywords that should never appear in detection SQL --

_FORBIDDEN_KEYWORDS_RE = re.compile(
    r"\b(?:INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE|MERGE)\b",
    re.IGNORECASE,
)

# String literals and quoted identifiers, with ClickHouse's doubled-quote and backslash escapes.
_QUOTES = "'`\""
_QUOTE_OPENERS = re.compile(r"['`\"]")
_QUOTE_STOPS = {quote: re.compile(rf"[{quote}\\]") for quote in _QUOTES}


def _quoted_span_end(sql: str, start: int) -> tuple[int | None, int]:
    r"""Where the span opened at ``start`` closes, and the index reading it stopped at.

    Reads as ``'(?:[^'\\]|\\.|'')*'`` reads, for each of the three quotes: a backslash
    escapes the next character unless that is a line break, a doubled quote stands for
    one, and a span with no lone closing quote ends at its last doubled one. The first
    value is the index after the closing quote, or None when the span never closes.
    """
    quote = sql[start]
    stops = _QUOTE_STOPS[quote]
    length = len(sql)
    last_doubled: int | None = None
    stopped = length
    pos = start + 1
    while (found := stops.search(sql, pos)) is not None:
        at = found.start()
        if sql[at] == "\\":
            if at + 1 < length and sql[at + 1] != "\n":
                pos = at + 2
                continue
            stopped = at
            break
        if at + 1 < length and sql[at + 1] == quote:
            last_doubled = at
            pos = at + 2
            continue
        return at + 1, at
    return (None if last_doubled is None else last_doubled + 1), stopped


def _mask_quoted(sql: str) -> str:
    """Blank the inside of every quoted span, keeping each character's position.

    One pass over the text. A span that never closes stopped reading where nothing of
    its kind could close any more, so an opener of that kind before that point is
    skipped rather than read to the same end again.
    """
    pieces: list[str] = []
    copied = 0
    unclosed_until = dict.fromkeys(_QUOTES, -1)
    pos = 0
    while (found := _QUOTE_OPENERS.search(sql, pos)) is not None:
        start = found.start()
        quote = sql[start]
        if start <= unclosed_until[quote]:
            pos = start + 1
            continue
        close, stopped = _quoted_span_end(sql, start)
        if close is None:
            unclosed_until[quote] = stopped
            pos = start + 1
            continue
        pieces.append(sql[copied : start + 1])
        pieces.append(" " * (close - start - 2))
        pieces.append(sql[close - 1])
        copied = pos = close
    pieces.append(sql[copied:])
    return "".join(pieces)


_EXPLAIN_PREFIX = "EXPLAIN AST "
_CH_POSITION_RE = re.compile(r"failed at position (\d+)")

_OFFBOX_FIX = "A rule matches rows of its own source table; take the call out of the SQL."
_ESCAPE_FIX = (
    "A rule matches rows of its own source table; compare against a list of values, "
    "not another table."
)
_CEL_FIX = (
    "Check the expression's syntax and that it calls only functions the hunt profile "
    "allows. To divide two fields, wrap an operand in double() or int()."
)


def _syntax_error(message: str) -> SqlValidationError:
    """ClickHouse's syntax error, with its 1-based position moved back onto the rule's SQL."""
    detail = message[message.find("Syntax error") :].splitlines()[0]
    detail = detail.split(" (version ")[0].strip()
    position: int | None = None
    found = _CH_POSITION_RE.search(detail)
    if found:
        position = max(int(found.group(1)) - 1 - len(_EXPLAIN_PREFIX), 0)
        detail = _CH_POSITION_RE.sub(f"failed at position {position + 1}", detail, count=1)
    return SqlValidationError(
        message=f"ClickHouse could not parse this SQL: {detail}",
        position=position,
        suggestion="Fix the syntax at the position ClickHouse reports.",
    )


def _source_error(source_db: str, source_table: str) -> SqlValidationError | None:
    """The FROM's database and table refused as the hunt runner refuses them, or None."""
    # The runner splices the stored pair into FROM, and drops a rule whose pair is not a table name.
    try:
        plain_source_name(rule_source(source_db, source_table))
    except ValueError as exc:
        return SqlValidationError(
            message=f"SQL's FROM names no table the hunt runner can scan: {exc}.",
            suggestion="Read from a table named in letters, digits, '_' and '-'.",
        )
    return None


# ClickHouse refusing the SQL itself: BAD_ARGUMENTS, NUMBER_OF_ARGUMENTS_DOESNT_MATCH,
# ILLEGAL_TYPE_OF_ARGUMENT, ILLEGAL_COLUMN, UNKNOWN_FUNCTION, UNKNOWN_IDENTIFIER,
# TYPE_MISMATCH, NO_COMMON_TYPE. A table not deployed yet (UNKNOWN_TABLE) does not block,
# since a rule may be saved ahead of its source.
_SQL_REFUSALS = frozenset({36, 42, 43, 44, 46, 47, 53, 386})

# ClickHouse error codes the preview names in its own words.
_TOO_MANY_ROWS = 158
_MEMORY_LIMIT_EXCEEDED = 241


def _failure_reason(
    exc: ClickHouseError, guard: DetectionGuardSettings, window: int, source: str
) -> str:
    """Why the preview query failed, in words a rule author can act on."""
    error = wrap_ch_error(exc)
    if error.category is ErrorCategory.TIMEOUT:
        return f"it ran past its {guard.preview_timeout_seconds:g} second limit on {source}"
    if error.code == _TOO_MANY_ROWS:
        return (
            f"the last {window} minutes of {source} are more than its "
            f"{guard.preview_max_rows:,} row limit"
        )
    if error.code == _MEMORY_LIMIT_EXCEEDED:
        return "it needed more memory than ClickHouse allows it"
    if error.category is ErrorCategory.CONNECTION:
        return "ClickHouse could not be reached"
    detail = str(exc).strip().splitlines()[0] if error.user_safe and str(exc).strip() else ""
    return f"ClickHouse refused it ({detail})" if detail else "ClickHouse refused it"


# -- Service ------------------------------------------------


class RuleCreationService:
    """Orchestrates rule creation from a source and its detection, or from raw or HyperDX SQL.

    Args:
        rewriter: Custom RuleRewriter instance (optional).
        hdx_sanitizer: Custom HdxSanitizer instance (optional).
        data_database: The database a source's table is read from when a rule
            is written as a condition.
        ch_config: ClickHouse config. With it, the parse check and the volume
            preview use the engine's pooled ClickHouse client.
        hunts: Hunt settings: the detection guard's thresholds and the load-time
            column the runner windows on. Defaults when omitted.
        ch_client: ClickHouse client for the parse check and the volume preview,
            in place of the pooled one.
        is_source: Whether a label names a registered source. A rule naming one
            that does not is refused. Omitted, no label is checked.
    """

    def __init__(
        self,
        *,
        rewriter: RuleRewriter | None = None,
        hdx_sanitizer: HdxSanitizer | None = None,
        data_database: str | None = None,
        ch_config: dict[str, Any] | None = None,
        hunts: HuntsSettings | None = None,
        ch_client: ChQueryClient | None = None,
        is_source: Callable[[str], bool] | None = None,
    ) -> None:
        self._rewriter = rewriter or RuleRewriter()
        self._hdx_sanitizer = hdx_sanitizer or HdxSanitizer()
        self._data_database = data_database
        self._ch_config = ch_config
        self._hunts = hunts or HuntsSettings()
        self._ch_client = ch_client
        self._is_source = is_source

    # -- Public API ----------------------------------------

    def create_rule(
        self,
        request: RuleCreateRequest,
        rule_id: str,
        *,
        tenant_ids: list[str] | None = None,
        plan: bool = True,
    ) -> RuleCreateResult:
        """Full pipeline: sanitize -> validate -> Rule.from_create -> guards -> AI stub.

        The volume preview runs only on a rule that would be saved. With the
        detection guard enabled its warnings come back as ``volume_warnings``,
        beside the rule and not in it, because a reading of the last hour is stale
        once the rule is saved. ``estimate_cost`` decides whether the measurement
        comes back as ``cost_estimate``.

        Args:
            request: Rule creation request with SQL and/or CEL filter.
            rule_id: Unique rule identifier.
            tenant_ids: The orgs the preview counts, by the tenant ids their rows
                carry. None counts every org, and an empty list leaves the rule
                unmeasured.
            plan: Whether ClickHouse resolves the condition against the FROM table.
                Off when that table is a stand-in for a source the hunt names later.

        Returns:
            RuleCreateResult with rule, validation errors, the match-everything
            verdict, cost estimate, and AI context.
        """
        try:
            request = self._with_rule_sql(request)
        except RuleConditionError as exc:
            return self._refused_condition(request, rule_id, str(exc))
        sanitize_summary: dict[str, Any] = {}
        clean_sql = request.user_sql
        refusal: SqlValidationError | None = None

        # Phase 1: HyperDX sanitization (if source_type="hyperdx")
        if request.source_type == "hyperdx" and request.user_sql:
            try:
                hdx_result = self._hdx_sanitizer.sanitize(request.user_sql)
            except HdxSanitizeError as exc:
                refusal = SqlValidationError(message=str(exc))
            else:
                clean_sql = hdx_result.clean_sql
                sanitize_summary = summarise_sanitized(hdx_result)
                logger.debug(
                    f"HdxSanitizer stripped {len(hdx_result.stripped_time_bounds)} "
                    f"time bounds, had_time_bucket={hdx_result.had_time_bucket}"
                )

        # Phase 2: SQL syntax validation, after the time placeholder the window replaces.
        if clean_sql:
            clean_sql = strip_time_placeholder(clean_sql)
        sql_errors: list[SqlValidationError] = []
        if refusal is not None:
            sql_errors = [refusal]
        elif request.user_sql:
            sql_errors = self._validate_sql_syntax(clean_sql or "", plan=plan)

        # Phase 3: Create Rule via existing pipeline. SQL the sanitiser emptied keeps
        # the caller's text so the result can carry the errors rather than raise.
        rule_create = RuleCreate(
            name=request.name,
            severity=request.severity,
            user_sql=clean_sql or request.user_sql,
            cel_filter=request.cel_filter,
            condition=request.condition,
            hunt_name=request.hunt_name,
            source=request.source,
        )
        try:
            rule = Rule.from_create(rule_create, rule_id=rule_id, rewriter=self._rewriter)
        except (ExpressionError, TranspileError) as exc:
            # The filter is the author's input, so its refusal is reported as refused SQL is.
            sql_errors = [
                SqlValidationError(message=f"CEL filter refused: {exc}", suggestion=_CEL_FIX),
                *sql_errors,
            ]
            rule = Rule(
                rule_id=rule_id,
                name=request.name,
                severity=request.severity,
                cel_filter=request.cel_filter,
                condition=request.condition,
                original_sql=clean_sql or request.user_sql or "",
                hunt_name=request.hunt_name,
                source=request.source,
            )

        # The stored WHERE is what the preview and the hunt runner execute, which a
        # transpiled CEL filter reaches without passing through the SQL check above.
        if not sql_errors:
            sql_errors = self.condition_errors(rule.where_clause)

        # Phase 4: a rule the API will refuse is neither judged nor measured further.
        refused = bool(sql_errors or rule.validate_rule())
        matches_everything = (
            None if refused else match_everything(rule.where_clause, source_label(rule))
        )

        # Phase 5: alert-volume preview
        guard = self._hunts.detection_guard
        cost_estimate: CostEstimate | None = None
        volume_warnings: list[str] = []
        measurable = not refused and matches_everything is None
        if measurable and request.measure_volume and rule.source_db and rule.source_table:
            if guard.enabled or request.estimate_cost:
                cost_estimate = self._preview_volume(rule, request.cost_window_minutes, tenant_ids)
            if guard.enabled and cost_estimate is not None:
                volume_warnings = list(cost_estimate.warnings)
            if not request.estimate_cost:
                cost_estimate = None
        json_path_warnings = (
            self._json_path_warnings(rule, tenant_ids)
            if measurable and rule.source_db and rule.source_table
            else []
        )

        # Phase 6: AI analysis stub
        ai_context = AIAnalysisStub(
            rule_id=rule_id,
            original_sql=request.user_sql or "",
            clean_sql=clean_sql or "",
            where_clause=rule.where_clause,
            source_table=rule.source_table,
            source_db=rule.source_db,
            cost_estimate=cost_estimate,
        )

        return RuleCreateResult(
            rule=rule,
            sanitize_summary=sanitize_summary,
            sql_errors=sql_errors,
            matches_everything=matches_everything,
            cost_estimate=cost_estimate,
            volume_warnings=volume_warnings,
            json_path_warnings=json_path_warnings,
            ai_context=ai_context,
        )

    def validate_sql(self, sql: str, *, plan: bool = True) -> list[SqlValidationError]:
        """Standalone SQL validation without rule creation.

        ``plan`` is off when the FROM table is a stand-in, so a column the landing
        table lacks is not a reason to refuse the SQL.
        """
        return self._validate_sql_syntax(strip_time_placeholder(sql), plan=plan)

    def condition_errors(self, where: str) -> list[SqlValidationError]:
        """What a stored WHERE may not call or read, which the preview and the runner execute.

        Args:
            where: The rule's detection WHERE.

        Returns:
            The refusal, or an empty list when the condition calls nothing outside the
            row and reads no other table.
        """
        offbox = refuse_offbox_calls(where)
        if offbox is not None:
            return [SqlValidationError(message=offbox, suggestion=_OFFBOX_FIX)]
        escape = refuse_condition_escape(where, lookup_databases=self._hunts.lookup_databases)
        if escape is not None:
            return [SqlValidationError(message=escape, suggestion=_ESCAPE_FIX)]
        return []

    def check_stored(
        self, source_db: str, source_table: str, where_clause: str
    ) -> list[SqlValidationError]:
        """The query check a stored rule passes when a hunt names it.

        Runs the refusals a save runs on the stored WHERE, then ClickHouse's plan of
        it against the source table. An unreachable ClickHouse leaves the plan unchecked.

        Args:
            source_db: The rule's stored database.
            source_table: The rule's stored table.
            where_clause: The rule's stored detection WHERE.

        Returns:
            Every reason the hunt runner could not run the rule, empty when none.
        """
        if not where_clause.strip():
            return [SqlValidationError(message="Rule has empty detection logic (WHERE clause).")]
        errors = self.condition_errors(where_clause)
        if errors or not source_db or not source_table:
            return errors
        plan_error = self._plan_error(source_db, source_table, where_clause)
        return [plan_error] if plan_error is not None else []

    def target_error(self, database: str, table: str) -> str | None:
        """Why a hunt's results table cannot take what the runner inserts, or None.

        Reads the table's columns from ``system.columns`` and compares them with the
        INSERT's. An unreachable ClickHouse leaves the table unchecked.

        Args:
            database: The target's database.
            table: The target's table.
        """
        client = self._query_client()
        if client is None:
            return None
        try:
            missing = missing_columns(client, database, table)
        except SchemaApplyError as exc:
            logger.warning(f"columns of {database}.{table} unavailable, target not checked: {exc}")
            return None
        if missing is None:
            return (
                f"{database}.{table} does not exist, so the runner has nowhere to write detections"
            )
        if missing:
            return f"{database}.{table} lacks the columns the runner writes: {', '.join(missing)}"
        return None

    def _with_rule_sql(self, request: RuleCreateRequest) -> RuleCreateRequest:
        """The request with ``user_sql`` set to the SELECT its source and detection compile to.

        A ``user_sql`` request is left as written. Where sources are checked, a source it
        names must be registered and its FROM must read that source's table. A request
        with no SQL of its own reads the table of its source.

        Raises:
            RuleConditionError: The source is missing, unregistered or not a table label,
                the SQL reads another table than its source, or the detection does not
                compile to a plain row filter.
        """
        if request.user_sql:
            if request.source and self._is_source is not None:
                self._refuse_another_table(request.user_sql, request.source)
            return request
        if request.condition is not None:
            sql = self.condition_sql(request.source, request.condition)
        elif request.where_clause is not None:
            sql = self.where_sql(request.source, request.where_clause)
        elif request.source:
            # A CEL filter alone: the rule reads its source's table, and the filter is the WHERE.
            target = self._quoted_target(*self._source_table(request.source))
            sql = f"SELECT * FROM {target}"  # noqa: S608 - quoted names
        else:
            return request
        return request.model_copy(update={"user_sql": sql})

    def _refuse_another_table(self, user_sql: str, source: str) -> None:
        """Refuse SQL whose FROM is not the registered table of the source it names.

        SQL that does not parse, or names no ``<db>.<table>``, is left to the SQL check.
        """
        database, table = self._source_table(source)
        parsed = self._rewriter.parse_user_sql(strip_time_placeholder(user_sql))
        if not (parsed.source_db and parsed.source_table):
            return
        if (parsed.source_db, parsed.source_table) != (database, table):
            raise RuleConditionError(
                f"the SQL reads {parsed.source_db}.{parsed.source_table}, but source "
                f"{source!r} is {database}.{table}"
            )

    def _refuse_unregistered(self, source: str | None) -> None:
        if source and self._is_source is not None and not self._is_source(source):
            raise RuleConditionError(
                f"source {source!r} is not a registered source, so no table backs the rule"
            )

    def _source_table(self, source: str | None) -> tuple[str, str]:
        """The data database and table a rule on ``source`` reads, once the source is registered.

        Raises:
            RuleConditionError: No source or data database, a source that is not a table
                label, or one that is not registered.
        """
        if not source:
            raise RuleConditionError("a rule reads a source's table, so name the source")
        if not self._data_database:
            raise RuleConditionError("no data database is configured to read the source from")
        try:
            database, table = plain_source_name(source)
        except ValueError as exc:
            raise RuleConditionError(f"source {source!r} is not a table label: {exc}") from None
        if database:
            raise RuleConditionError(f"source {source!r} is a label, not a <db>.<table> name")
        self._refuse_unregistered(source)
        return self._data_database, table

    @staticmethod
    def _quoted_target(database: str, table: str) -> str:
        return f"{quote_identifier(database)}.{quote_identifier(table)}"

    def where_sql(self, source: str | None, where_clause: str) -> str:
        """The rule SQL a WHERE clause on ``source`` compiles to.

        Args:
            source: The source's label, which is also its table's name.
            where_clause: The row filter, written as the part after WHERE.

        Returns:
            ``SELECT * FROM <db>.<table> WHERE <where_clause>``, the names quoted.

        Raises:
            RuleConditionError: The source does not resolve, the WHERE is empty, carries a
                ``{...}`` placeholder or a time bound the hunt window replaces, or holds
                more than a row filter.
        """
        where = where_clause.strip()
        if not where:
            raise RuleConditionError(
                "the WHERE clause is empty, and a rule needs one to select rows"
            )
        placeholders = find_placeholders(where)
        if placeholders:
            raise RuleConditionError(
                f"the WHERE clause has {', '.join(placeholders)}. The time window comes from "
                "the hunt schedule, and the runner fills in no placeholder"
            )
        target = self._quoted_target(*self._source_table(source))
        sql = f"SELECT * FROM {target} WHERE {where}"  # noqa: S608 - quoted names, and the WHERE is the rule and read back below
        # What is stored must be what the runner runs, so a bound or clause the read drops is refused.
        try:
            split = split_time_window(sql, TIMESTAMP_COLUMNS)
        except HdxSanitizeError as exc:
            raise RuleConditionError(f"the WHERE clause could not be read: {exc}") from None
        if split.removed:
            raise RuleConditionError(
                "the hunt window bounds time for every rule, so the WHERE cannot compare a "
                f"time column with lt, lte, gt or gte: {', '.join(split.removed)}"
            )
        if split.after_filter.strip() or split.ignored_clauses:
            raise RuleConditionError(
                "the WHERE clause holds more than a row filter, such as another clause or statement"
            )
        return sql

    def condition_sql(self, source: str | None, condition: RuleCondition) -> str:
        """The rule SQL a condition on ``source`` compiles to.

        The condition reads the source's table in the data database. A field is a
        column of that table when ClickHouse reports one by that name, else a path
        inside its ``_json`` column.

        Args:
            source: The source's label, which is also its table's name.
            condition: A validated condition.

        Returns:
            ``SELECT * FROM <db>.<table> WHERE <condition>``, every name quoted.

        Raises:
            RuleConditionError: No source or data database to read, a source that
                is not a registered table label, or a field that does not fit the table.
        """
        database, table = self._source_table(source)
        columns = self._table_columns(database, table)
        where = compile_condition(condition, columns)
        target = self._quoted_target(database, table)
        # A negated time bound survives the rule read below, so each ordering test is read on its own.
        bounds = [
            bound
            for test in condition.tests()
            if test.operator in ORDERINGS
            for bound in self._time_bounds(
                target, compile_condition(test.model_copy(update={"negate": False}), columns)
            )
        ]
        sql = f"SELECT * FROM {target} WHERE {where}"  # noqa: S608 - quoted names, and operands quoted by compile_condition
        if len(sql.encode()) > MAX_CONDITION_SQL_BYTES:
            raise RuleConditionError(
                f"the condition compiles to more than {MAX_CONDITION_SQL_BYTES} bytes of SQL, "
                "which leaves the runner no room under ClickHouse's query size limit"
            )
        # What the stored condition says must be what the runner runs, so a rule read of the SQL may not change it.
        parsed = self._rewriter.parse_user_sql(sql)
        bounds = bounds or parsed.stripped_time_bounds
        if bounds:
            raise RuleConditionError(
                "the hunt window bounds time for every rule, so a condition cannot compare a "
                f"time column with lt, lte, gt or gte: {', '.join(bounds)}"
            )
        if parsed.where_clause != where:
            raise RuleConditionError("reading the compiled condition as a rule changed it")
        return sql

    def _time_bounds(self, target: str, where: str) -> list[str]:
        """The time bounds a rule read takes out of ``where`` on ``target``."""
        sql = f"SELECT * FROM {target} WHERE {where}"  # noqa: S608 - parsed only, never run
        return self._rewriter.parse_user_sql(sql).stripped_time_bounds

    def _table_columns(self, database: str, table: str) -> frozenset[str] | None:
        """The table's column names, or None when no ClickHouse is configured or the table is absent.

        Raises:
            RuleConditionError: ClickHouse is configured and could not be read, so a
                column cannot be told from a JSON path and compiling would guess.
        """
        client = self._query_client()
        if client is None:
            return None
        try:
            columns = live_columns(client=client, database=database, table=table)
        except SchemaApplyError as exc:
            logger.warning(f"columns of {database}.{table} unavailable: {exc}")
            raise RuleConditionError(
                f"ClickHouse could not be read for the columns of {database}.{table}, so a "
                "field cannot be told from a JSON path. Retry shortly"
            ) from None
        return frozenset(columns) or None

    @staticmethod
    def _refused_condition(
        request: RuleCreateRequest, rule_id: str, message: str
    ) -> RuleCreateResult:
        """The result for a condition that does not compile, which the caller refuses on its error."""
        rule = Rule(
            rule_id=rule_id,
            name=request.name,
            severity=request.severity,
            cel_filter=request.cel_filter,
            condition=request.condition,
            hunt_name=request.hunt_name,
            source=request.source,
        )
        return RuleCreateResult(rule=rule, sql_errors=[SqlValidationError(message=message)])

    # -- Internal methods ----------------------------------

    def _validate_sql_syntax(self, sql: str, *, plan: bool = True) -> list[SqlValidationError]:
        """Structural SQL validation with helpful error messages.

        Checks:
        1. Must start with SELECT
        2. Must contain FROM
        3. No DDL/DML keywords (INSERT, DROP, etc.)
        4. Balanced parentheses
        5. Parses as one ClickHouse SELECT (EXPLAIN AST when ClickHouse is
           configured, else sqlglot's ClickHouse dialect)
        6. Its FROM names a ``<db>.<table>``, read the way the rule model reads it,
           in the characters a source table name may carry
        7. No clause a row filter cannot carry (HAVING, a join, a union, ...),
           refused as the HyperDX sanitiser refuses it
        8. No call that reads outside the row (``url``, ``s3``, a dictionary) or
           sends it to another service (the ``ai*`` functions)
        9. A condition that reads no other table: no subquery, no ``IN <table>``,
           except a lookup in one of ``hunts.lookup_databases``
        """
        errors: list[SqlValidationError] = []
        trimmed = sql.strip()
        # Keywords and parentheses inside a literal are data, not structure.
        structure = _mask_quoted(trimmed)

        if not trimmed.upper().startswith("SELECT"):
            errors.append(
                SqlValidationError(
                    message="SQL must start with SELECT.",
                    position=0,
                    suggestion="Ensure your query begins with 'SELECT ...'",
                )
            )

        if not re.search(r"\bFROM\b", structure, re.IGNORECASE):
            errors.append(
                SqlValidationError(
                    message="SQL must contain a FROM clause.",
                    suggestion="Add 'FROM <table>' to specify the data source.",
                )
            )

        ddl_match = _FORBIDDEN_KEYWORDS_RE.search(structure)
        if ddl_match:
            keyword = ddl_match.group(0).upper()
            errors.append(
                SqlValidationError(
                    message=f"SQL contains forbidden keyword '{keyword}'.",
                    position=ddl_match.start(),
                    suggestion="Detection rules must be SELECT queries only.",
                )
            )

        # Balanced parentheses
        depth = 0
        for i, ch in enumerate(structure):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            if depth < 0:
                errors.append(
                    SqlValidationError(
                        message="Unmatched closing parenthesis.",
                        position=i,
                        suggestion="Check parentheses around WHERE conditions.",
                    )
                )
                break
        if depth > 0:
            errors.append(
                SqlValidationError(
                    message=f"Unmatched opening parenthesis ({depth} unclosed).",
                    suggestion="Check parentheses around WHERE conditions.",
                )
            )

        offbox = refuse_offbox_calls(trimmed, subject="A rule's SQL")
        if offbox is not None:
            errors.append(SqlValidationError(message=offbox, suggestion=_OFFBOX_FIX))

        # The keyword checks above read like a lint. Only a parse says the SQL is SQL.
        if not errors:
            parse_error = self._parse_error(trimmed)
            if parse_error is not None:
                errors.append(parse_error)

        # The hunt runner scans the rule's own <db>.<table>, and an unqualified one
        # compiles to no source at all.
        if not errors:
            parsed = self._rewriter.parse_user_sql(trimmed)
            if not (parsed.source_db and parsed.source_table):
                errors.append(
                    SqlValidationError(
                        message="SQL names no <db>.<table> source for the hunt runner to scan.",
                        suggestion="Qualify the FROM table with its database, e.g. FROM dfe.main.",
                    )
                )
            else:
                source_error = _source_error(parsed.source_db, parsed.source_table)
                if source_error is not None:
                    errors.append(source_error)
            # A rule stores only the row filter, so a clause beyond it would be dropped at save.
            errors.extend(
                SqlValidationError(
                    message=REFUSED_CLAUSES[clause],
                    suggestion="A rule matches single rows of one table.",
                )
                for clause in parsed.ignored_clauses
                if clause in REFUSED_CLAUSES
            )
            escape = refuse_condition_escape(
                parsed.where_clause,
                subject="A rule's condition",
                lookup_databases=self._hunts.lookup_databases,
            )
            if escape is not None:
                errors.append(SqlValidationError(message=escape, suggestion=_ESCAPE_FIX))
            if not errors and plan:
                plan_error = self._plan_error(
                    parsed.source_db or "", parsed.source_table or "", parsed.where_clause
                )
                if plan_error is not None:
                    errors.append(plan_error)

        return errors

    def _plan_error(self, db: str, table: str, where_clause: str) -> SqlValidationError | None:
        """ClickHouse's refusal of the condition against the source table, or None.

        ``EXPLAIN PLAN`` resolves every identifier against the table, which the parse
        check does not, so a misspelt column or function fails here and not at the
        first hunt run. Only a refusal about the SQL blocks: an unreachable or busy
        ClickHouse leaves the rule to the parse check alone.
        """
        client = self._query_client()
        if client is None:
            return None
        target = f"{quote_identifier(db)}.{quote_identifier(table)}"
        condition = f" WHERE {where_clause}" if where_clause.strip() else ""
        try:
            # command(), not query(), as in _parse_error. EXPLAIN PLAN analyses the SELECT and never reads rows.
            # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query, python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query
            client.command(f"EXPLAIN PLAN SELECT 1 FROM {target}{condition}")  # noqa: S608 - quoted source, the condition has passed the sanitiser and EXPLAIN never runs it
        except Exception as exc:
            if parse_code(exc) not in _SQL_REFUSALS:
                logger.warning(f"EXPLAIN PLAN unavailable, rule not checked against {db}.{table}")
                return None
            detail = str(exc).splitlines()[0]
            return SqlValidationError(
                message=f"ClickHouse cannot plan this rule against {db}.{table}: {detail}",
                suggestion="Check the column, function and table names against the source.",
            )
        return None

    def _parse_error(self, sql: str) -> SqlValidationError | None:
        """Parse the SQL as ClickHouse and return the first syntax error, if any.

        ClickHouse's own parser (``EXPLAIN AST``) is authoritative when it is
        configured and reachable. sqlglot's ClickHouse dialect stands in otherwise.
        """
        client = self._query_client()
        if client is not None:
            try:
                # command(), not query(): query() appends FORMAT Native, which binds to the explained SELECT and not to EXPLAIN's own output.
                # Risk accepted: EXPLAIN AST only asks ClickHouse to parse the SQL, never to run it, and the SQL has already passed the sanitiser.
                # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query, python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query
                client.command(f"{_EXPLAIN_PREFIX}{sql}")
                return None
            except Exception as exc:
                if "Syntax error" in str(exc):
                    return _syntax_error(str(exc))
                detail = str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__
                logger.warning(f"EXPLAIN AST unavailable, parsing with sqlglot: {detail}")

        try:
            statements = sqlglot.parse(sql, read="clickhouse")
        except ParseError as exc:
            first = exc.errors[0] if exc.errors else {}
            description = str(first.get("description") or exc).strip()
            line = first.get("line")
            col = first.get("col")
            position = (col - 1) if line == 1 and isinstance(col, int) else None
            return SqlValidationError(
                message=f"SQL does not parse as ClickHouse: {description}",
                position=position,
                suggestion="Fix the syntax at the reported position.",
            )
        statements = [s for s in statements if s is not None]
        if len(statements) != 1:
            return SqlValidationError(
                message=f"Expected one SELECT statement, found {len(statements)}.",
                suggestion="A detection rule is a single SELECT.",
            )
        return None

    def _query_client(self) -> ChQueryClient | None:
        """The client the parse check and preview use, or None when no ClickHouse is configured."""
        if self._ch_client is not None:
            return self._ch_client
        if not self._ch_config:
            return None
        from ..clickhouse.clickhouse_manager import ClickHouseManager

        return ClickHouseManager.get_instance().get_clickhouse_client()

    def _json_path_warnings(self, rule: Rule, tenant_ids: list[str] | None) -> list[str]:
        """Warn for each ``_json`` path the rule reads that recent rows of its source never carried.

        The read is bounded in rows, time and orgs, runs read-only, and breaks off rather than
        fails, since a partial read still names the paths it saw. An unreachable ClickHouse,
        an empty window or a read held to no org leaves the paths unchecked.
        """
        referenced = referenced_json_paths(rule.where_clause)
        client = self._query_client()
        if not referenced or client is None or (tenant_ids is not None and not tenant_ids):
            return []
        guard = self._hunts.detection_guard
        window = guard.preview_window_minutes
        sql = seen_paths_sql(
            rule.source_db or "",
            rule.source_table or "",
            self._hunts.checkpoint_timestamp_field,
            window,
            tenant_ids=tenant_ids,
        )
        settings = {
            **preview_settings(guard),
            "read_overflow_mode": "break",
            "timeout_overflow_mode": "break",
        }
        try:
            rows = client.query(sql, settings=settings).result_rows
        except (ServiceUnavailable, ClickHouseError) as exc:
            logger.warning(f"JSON paths of {source_label(rule)} unavailable: {exc}")
            return []
        seen = list(rows[0][0]) if rows and rows[0][0] else []
        if not seen:
            return []
        missing = unseen_paths(referenced, seen)
        return [unseen_warning(missing, source_label(rule), window)] if missing else []

    def _preview_volume(
        self, rule: Rule, window_minutes: int | None, tenant_ids: list[str] | None
    ) -> CostEstimate:
        """Count what the rule matches over the lookback window and band the result.

        A preview that cannot finish -- timed out, over its row budget, ClickHouse
        unreachable or refusing -- comes back UNMEASURED with a warning saying so,
        never as a rule that matches nothing. So does one held to no org.
        """
        guard = self._hunts.detection_guard
        window = guard.preview_window_minutes
        if window_minutes is not None and window_minutes > 0:
            window = window_minutes
        source = source_label(rule)

        def unmeasured(reason: str) -> CostEstimate:
            logger.warning(
                "Rule volume preview did not finish", rule_id=rule.rule_id, reason=reason
            )
            return CostEstimate(window_minutes=window, warnings=[unmeasured_warning(reason)])

        if tenant_ids is not None and not tenant_ids:
            return unmeasured("it may count no org's rows for this rule")
        client = self._query_client()
        if client is None:
            return unmeasured("no ClickHouse is configured")

        sql = preview_sql(
            rule.source_db or "",
            rule.source_table or "",
            self._hunts.checkpoint_timestamp_field,
            rule.where_clause,
            window,
            tenant_ids=tenant_ids,
        )
        started = time.monotonic()
        try:
            rows = client.query(sql, settings=preview_settings(guard)).result_rows
        except ServiceUnavailable:
            return unmeasured("ClickHouse could not be reached")
        except ClickHouseError as exc:
            return unmeasured(_failure_reason(exc, guard, window, source))
        duration_ms = (time.monotonic() - started) * 1000
        if not rows:
            return unmeasured("ClickHouse returned no count")

        total, matched = (int(value) for value in rows[0])
        measure = VolumeMeasure(window_minutes=window, total=total, matched=matched)
        band, warnings = volume_verdict(measure, guard, source)
        logger.info(
            "Rule volume preview",
            rule_id=rule.rule_id,
            window_minutes=window,
            total=total,
            matched=matched,
            per_day=measure.per_day,
            band=str(band),
        )
        return CostEstimate(
            estimated_rows=matched,
            rows_in_window=total,
            projected_per_day=measure.per_day if total else None,
            match_ratio=measure.ratio,
            band=band,
            window_minutes=window,
            duration_ms=duration_ms,
            warnings=warnings,
        )


def service_from_settings(
    settings: Any, *, is_source: Callable[[str], bool] | None = None
) -> RuleCreationService:
    """The creation service every rule and hunt check runs through, on the engine's own ClickHouse.

    Args:
        settings: The engine settings.
        is_source: Whether a label names a registered source.
    """
    from ..settings import get_clickhouse_config

    return RuleCreationService(
        ch_config=get_clickhouse_config(settings),
        hunts=settings.hunts,
        data_database=settings.clickhouse.effective_data_database,
        is_source=is_source,
    )
