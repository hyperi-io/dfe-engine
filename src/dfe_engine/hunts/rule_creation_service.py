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
            source_type="hyperdx",
            user_sql="SELECT count() FROM default.logs WHERE ...",
        ),
        rule_id="win_cert_01",
    )
    # result.rule -- Rule instance
    # result.sql_errors -- validation issues
    # result.sanitize_summary -- what was stripped
"""

import re
import time
from typing import Any, Literal, Protocol

import sqlglot
from clickhouse_connect.driver.exceptions import ClickHouseError
from pydantic import BaseModel, Field, model_validator
from scalo.logger import logger
from scalo.resilience import ServiceUnavailable
from sqlglot.errors import ParseError

from ..clickhouse.errors import ErrorCategory, wrap_ch_error
from ..settings import DetectionGuardSettings, HuntsSettings
from .hdx_sanitizer import REFUSED_CLAUSES, HdxSanitizeError, HdxSanitizer, HdxSanitizeResult
from .rule_guard import (
    VolumeBand,
    VolumeMeasure,
    match_everything,
    preview_settings,
    preview_sql,
    refuse_offbox_calls,
    source_label,
    unmeasured_warning,
    volume_verdict,
)
from .rule_model import Rule, RuleCreate
from .rule_rewriter import RuleRewriter, strip_time_placeholder

# -- Request / Response models ------------------------------


class RuleCreateRequest(BaseModel):
    """Input for the rule creation pipeline.

    At least one of ``user_sql`` or ``cel_filter`` must be provided.
    ``source_type`` controls whether HyperDX sanitization is applied.
    """

    name: str = Field(..., description="Human-readable rule name")
    severity: str = Field(default="medium", description="Detection severity")
    source_type: Literal["raw", "hyperdx"] = Field(
        default="raw",
        description="SQL origin: 'raw' for clean SQL, 'hyperdx' for HyperDX-generated",
    )
    user_sql: str | None = Field(default=None, description="User-supplied SQL SELECT")
    cel_filter: str | None = Field(default=None, description="CEL expression for query pushdown")
    hunt_name: str | None = Field(default=None, description="Parent hunt name")
    source: str | None = Field(
        default=None, description="Source name for SourceRegistry resolution"
    )
    estimate_cost: bool = Field(
        default=True,
        description="Return the measured alert-volume preview in the result's cost_estimate",
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
        if not self.user_sql and not self.cel_filter:
            raise ValueError("At least one of 'user_sql' or 'cel_filter' must be provided.")
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

    def command(self, cmd: str, *args: Any, **kwargs: Any) -> Any:
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
    ai_context: AIAnalysisStub | None = None


# -- DDL/DML keywords that should never appear in detection SQL --

_FORBIDDEN_KEYWORDS_RE = re.compile(
    r"\b(?:INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE|MERGE)\b",
    re.IGNORECASE,
)

# String literals and quoted identifiers, with ClickHouse's doubled-quote and backslash escapes.
_QUOTED_RE = re.compile(r"'(?:[^'\\]|\\.|'')*'|`(?:[^`\\]|\\.|``)*`|\"(?:[^\"\\]|\\.|\"\")*\"")


def _blank_inside(match: re.Match[str]) -> str:
    quoted = match.group(0)
    return quoted[0] + " " * (len(quoted) - 2) + quoted[-1]


def _mask_quoted(sql: str) -> str:
    """Blank the inside of every quoted span, keeping each character's position."""
    return _QUOTED_RE.sub(_blank_inside, sql)


_EXPLAIN_PREFIX = "EXPLAIN AST "
_CH_POSITION_RE = re.compile(r"failed at position (\d+)")

_OFFBOX_FIX = "A rule matches rows of its own source table; take the call out of the SQL."


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
    """Orchestrates rule creation from raw or HyperDX SQL.

    Args:
        rewriter: Custom RuleRewriter instance (optional).
        hdx_sanitizer: Custom HdxSanitizer instance (optional).
        ch_config: ClickHouse config. With it, the parse check and the volume
            preview use the engine's pooled ClickHouse client.
        hunts: Hunt settings: the detection guard's thresholds and the load-time
            column the runner windows on. Defaults when omitted.
        ch_client: ClickHouse client for the parse check and the volume preview,
            in place of the pooled one.
    """

    def __init__(
        self,
        *,
        rewriter: RuleRewriter | None = None,
        hdx_sanitizer: HdxSanitizer | None = None,
        ch_config: dict[str, Any] | None = None,
        hunts: HuntsSettings | None = None,
        ch_client: ChQueryClient | None = None,
    ) -> None:
        self._rewriter = rewriter or RuleRewriter()
        self._hdx_sanitizer = hdx_sanitizer or HdxSanitizer()
        self._ch_config = ch_config
        self._hunts = hunts or HuntsSettings()
        self._ch_client = ch_client

    # -- Public API ----------------------------------------

    def create_rule(
        self,
        request: RuleCreateRequest,
        rule_id: str,
    ) -> RuleCreateResult:
        """Full pipeline: sanitize -> validate -> Rule.from_create -> guards -> AI stub.

        The volume preview runs only on a rule that would be saved. With the
        detection guard enabled its warnings join the rule's own, and
        ``estimate_cost`` decides whether the measurement comes back as
        ``cost_estimate``.

        Args:
            request: Rule creation request with SQL and/or CEL filter.
            rule_id: Unique rule identifier.

        Returns:
            RuleCreateResult with rule, validation errors, the match-everything
            verdict, cost estimate, and AI context.
        """
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
                sanitize_summary = self._build_sanitize_summary(hdx_result)
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
            sql_errors = self._validate_sql_syntax(clean_sql or "")

        # Phase 3: Create Rule via existing pipeline; SQL the sanitiser emptied keeps
        # the caller's text so the result can carry the errors rather than raise.
        rule_create = RuleCreate(
            name=request.name,
            severity=request.severity,
            user_sql=clean_sql or request.user_sql,
            cel_filter=request.cel_filter,
            hunt_name=request.hunt_name,
            source=request.source,
        )
        rule = Rule.from_create(rule_create, rule_id=rule_id, rewriter=self._rewriter)

        # The stored WHERE is what the preview and the hunt runner execute, which a
        # transpiled CEL filter reaches without passing through the SQL check above.
        if not sql_errors:
            offbox = refuse_offbox_calls(rule.where_clause)
            if offbox is not None:
                sql_errors = [SqlValidationError(message=offbox, suggestion=_OFFBOX_FIX)]

        # Phase 4: a rule the API will refuse is neither judged nor measured further.
        refused = bool(sql_errors or rule.validate_rule())
        matches_everything = (
            None if refused else match_everything(rule.where_clause, source_label(rule))
        )

        # Phase 5: alert-volume preview
        guard = self._hunts.detection_guard
        cost_estimate: CostEstimate | None = None
        measurable = not refused and matches_everything is None
        if measurable and rule.source_db and rule.source_table:
            if guard.enabled or request.estimate_cost:
                cost_estimate = self._preview_volume(rule, request.cost_window_minutes)
            if guard.enabled and cost_estimate is not None:
                rule.warnings.extend(cost_estimate.warnings)
            if not request.estimate_cost:
                cost_estimate = None

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
            ai_context=ai_context,
        )

    def validate_sql(self, sql: str) -> list[SqlValidationError]:
        """Standalone SQL validation without rule creation."""
        return self._validate_sql_syntax(strip_time_placeholder(sql))

    # -- Internal methods ----------------------------------

    def _validate_sql_syntax(self, sql: str) -> list[SqlValidationError]:
        """Structural SQL validation with helpful error messages.

        Checks:
        1. Must start with SELECT
        2. Must contain FROM
        3. No DDL/DML keywords (INSERT, DROP, etc.)
        4. Balanced parentheses
        5. Parses as one ClickHouse SELECT (EXPLAIN AST when ClickHouse is
           configured, else sqlglot's ClickHouse dialect)
        6. Its FROM names a ``<db>.<table>``, read the way the rule model reads it
        7. No clause a row filter cannot carry (HAVING, a join, a union, ...),
           refused as the HyperDX sanitiser refuses it
        8. No call that reads outside the row (``url``, ``s3``, a dictionary) or
           sends it to another service (the ``ai*`` functions)
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

        # The keyword checks above read like a lint; only a parse says the SQL is SQL.
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
            # A rule stores only the row filter, so a clause beyond it would be dropped at save.
            errors.extend(
                SqlValidationError(
                    message=REFUSED_CLAUSES[clause],
                    suggestion="A rule matches single rows of one table.",
                )
                for clause in parsed.ignored_clauses
                if clause in REFUSED_CLAUSES
            )

        return errors

    def _parse_error(self, sql: str) -> SqlValidationError | None:
        """Parse the SQL as ClickHouse and return the first syntax error, if any.

        ClickHouse's own parser (``EXPLAIN AST``) is authoritative when it is
        configured and reachable; sqlglot's ClickHouse dialect stands in otherwise.
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

    def _preview_volume(self, rule: Rule, window_minutes: int | None) -> CostEstimate:
        """Count what the rule matches over the lookback window and band the result.

        A preview that cannot finish -- timed out, over its row budget, ClickHouse
        unreachable or refusing -- comes back UNMEASURED with a warning saying so,
        never as a rule that matches nothing.
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

        client = self._query_client()
        if client is None:
            return unmeasured("no ClickHouse is configured")

        sql = preview_sql(
            rule.source_db or "",
            rule.source_table or "",
            self._hunts.checkpoint_timestamp_field,
            rule.where_clause,
            window,
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

    @staticmethod
    def _build_sanitize_summary(hdx_result: HdxSanitizeResult) -> dict[str, Any]:
        """Convert HdxSanitizeResult to a summary dict for API response."""
        summary: dict[str, Any] = {}

        if hdx_result.stripped_time_bounds:
            summary["stripped_time_bounds"] = hdx_result.stripped_time_bounds
        if hdx_result.stripped_settings:
            summary["stripped_settings"] = hdx_result.stripped_settings
        if hdx_result.stripped_limit:
            summary["stripped_limit"] = hdx_result.stripped_limit
        if hdx_result.stripped_time_bucket_select:
            summary["stripped_time_bucket_select"] = hdx_result.stripped_time_bucket_select
        if hdx_result.stripped_time_bucket_refs:
            summary["stripped_time_bucket_refs"] = hdx_result.stripped_time_bucket_refs
        if hdx_result.had_time_bucket:
            summary["had_time_bucket"] = True
        if hdx_result.stripped_clauses:
            summary["stripped_clauses"] = hdx_result.stripped_clauses
        if hdx_result.warnings:
            summary["warnings"] = hdx_result.warnings

        return summary
