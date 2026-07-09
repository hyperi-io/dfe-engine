"""Rule Creation Service — HyperDX-aware rule creation pipeline.

Orchestrates the full rule creation workflow:
1. HyperDX SQL sanitization (if source_type="hyperdx")
2. SQL syntax validation with helpful error messages
3. Rule creation via Rule.from_create()
4. Optional EXPLAIN cost estimation against ClickHouse
5. AI analysis stub for async TS runner consumption

dfe-engine is a library — no HTTP endpoints here. The control-plane
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
    # result.rule — Rule instance
    # result.sql_errors — validation issues
    # result.sanitize_summary — what was stripped
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator
from scalo.logger import logger

from .hdx_sanitizer import HdxSanitizer, HdxSanitizeResult
from .rule_model import Rule, RuleCreate
from .rule_rewriter import RuleRewriter

# ── Request / Response models ──────────────────────────────


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
    estimate_cost: bool = Field(default=False, description="Run EXPLAIN to estimate query cost")
    cost_window_minutes: int = Field(
        default=10, description="Time window (minutes) for cost estimation"
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
    """EXPLAIN-based query cost estimate."""

    estimated_rows: int | None = None
    explain_plan: str | None = None
    explain_duration_ms: float | None = None
    window_minutes: int = 10
    warnings: list[str] = Field(default_factory=list)


class AIAnalysisStub(BaseModel):
    """Context for async AI analysis — consumed by TS runner, not processed here."""

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
    cost_estimate: CostEstimate | None = None
    ai_context: AIAnalysisStub | None = None


# ── DDL/DML keywords that should never appear in detection SQL ──

_FORBIDDEN_KEYWORDS_RE = re.compile(
    r"\b(?:INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE|MERGE)\b",
    re.IGNORECASE,
)


# ── Service ────────────────────────────────────────────────


class RuleCreationService:
    """Orchestrates rule creation from raw or HyperDX SQL.

    Args:
        rewriter: Custom RuleRewriter instance (optional).
        hdx_sanitizer: Custom HdxSanitizer instance (optional).
        ch_config: ClickHouse config for cost estimation (optional).
            When None, cost estimation is skipped gracefully.
    """

    def __init__(
        self,
        *,
        rewriter: RuleRewriter | None = None,
        hdx_sanitizer: HdxSanitizer | None = None,
        ch_config: dict[str, Any] | None = None,
    ) -> None:
        self._rewriter = rewriter or RuleRewriter()
        self._hdx_sanitizer = hdx_sanitizer or HdxSanitizer()
        self._ch_config = ch_config

    # ── Public API ────────────────────────────────────────

    def create_rule(
        self,
        request: RuleCreateRequest,
        rule_id: str,
    ) -> RuleCreateResult:
        """Full pipeline: sanitize -> validate -> Rule.from_create -> EXPLAIN -> AI stub.

        Args:
            request: Rule creation request with SQL and/or CEL filter.
            rule_id: Unique rule identifier.

        Returns:
            RuleCreateResult with rule, validation errors, cost estimate, and AI context.
        """
        sanitize_summary: dict[str, Any] = {}
        clean_sql = request.user_sql

        # Phase 1: HyperDX sanitization (if source_type="hyperdx")
        if request.source_type == "hyperdx" and request.user_sql:
            hdx_result = self._hdx_sanitizer.sanitize(request.user_sql)
            clean_sql = hdx_result.clean_sql
            sanitize_summary = self._build_sanitize_summary(hdx_result)
            logger.debug(
                f"HdxSanitizer stripped {len(hdx_result.stripped_time_bounds)} "
                f"time bounds, had_time_bucket={hdx_result.had_time_bucket}"
            )

        # Phase 2: SQL syntax validation
        sql_errors: list[SqlValidationError] = []
        if clean_sql:
            sql_errors = self._validate_sql_syntax(clean_sql)

        # Phase 3: Create Rule via existing pipeline
        rule_create = RuleCreate(
            name=request.name,
            severity=request.severity,
            user_sql=clean_sql,
            cel_filter=request.cel_filter,
            hunt_name=request.hunt_name,
            source=request.source,
        )
        rule = Rule.from_create(rule_create, rule_id=rule_id, rewriter=self._rewriter)

        # Phase 4: Cost estimation (optional, best-effort)
        cost_estimate: CostEstimate | None = None
        if request.estimate_cost and rule.source_table:
            cost_estimate = self._estimate_cost(rule, request.cost_window_minutes)

        # Phase 5: AI analysis stub
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
            cost_estimate=cost_estimate,
            ai_context=ai_context,
        )

    def validate_sql(self, sql: str) -> list[SqlValidationError]:
        """Standalone SQL validation without rule creation."""
        return self._validate_sql_syntax(sql)

    # ── Internal methods ──────────────────────────────────

    def _validate_sql_syntax(self, sql: str) -> list[SqlValidationError]:
        """Structural SQL validation with helpful error messages.

        Checks:
        1. Must start with SELECT
        2. Must contain FROM
        3. No DDL/DML keywords (INSERT, DROP, etc.)
        4. Balanced parentheses
        """
        errors: list[SqlValidationError] = []
        trimmed = sql.strip()

        if not trimmed.upper().startswith("SELECT"):
            errors.append(
                SqlValidationError(
                    message="SQL must start with SELECT.",
                    position=0,
                    suggestion="Ensure your query begins with 'SELECT ...'",
                )
            )

        if not re.search(r"\bFROM\b", trimmed, re.IGNORECASE):
            errors.append(
                SqlValidationError(
                    message="SQL must contain a FROM clause.",
                    suggestion="Add 'FROM <table>' to specify the data source.",
                )
            )

        ddl_match = _FORBIDDEN_KEYWORDS_RE.search(trimmed)
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
        for i, ch in enumerate(trimmed):
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

        return errors

    def _estimate_cost(self, rule: Rule, window_minutes: int) -> CostEstimate:
        """Run EXPLAIN PLAN against ClickHouse. Best-effort, never blocks.

        Follows the same EXPLAIN pattern as Hunt._execute_single_rule().
        """
        warnings: list[str] = []

        if not self._ch_config:
            warnings.append("ClickHouse not configured — cost estimation skipped.")
            return CostEstimate(window_minutes=window_minutes, warnings=warnings)

        try:
            from ..clickhouse.clickhouse_manager import ClickHouseManager

            ch = ClickHouseManager.get_instance()
            client = ch.get_clickhouse_client()

            # Build a minimal SELECT to estimate cost
            db_table = (
                f"{rule.source_db}.{rule.source_table}" if rule.source_db else rule.source_table
            )
            where = rule.where_clause.strip()
            if where:
                explain_sql = f"EXPLAIN ESTIMATE SELECT count() FROM {db_table} WHERE {where}"
            else:
                explain_sql = f"EXPLAIN ESTIMATE SELECT count() FROM {db_table}"

            explain_start = datetime.now(UTC)
            explain_result = client.execute(explain_sql)
            explain_duration_ms = (datetime.now(UTC) - explain_start).total_seconds() * 1000

            explain_plan = "\n".join(
                str(row[0]) if isinstance(row, (list, tuple)) else str(row)
                for row in explain_result
            )

            # Try to extract estimated rows from EXPLAIN output
            estimated_rows = None
            for row in explain_result:
                row_str = str(row[0]) if isinstance(row, (list, tuple)) else str(row)
                row_match = re.search(r"estimated_rows[=:]\s*(\d+)", row_str)
                if row_match:
                    estimated_rows = int(row_match.group(1))
                    break

            logger.info(
                f"EXPLAIN ESTIMATE [{rule.rule_id}]: "
                f"rows={estimated_rows}, {explain_duration_ms:.0f}ms"
            )

            return CostEstimate(
                estimated_rows=estimated_rows,
                explain_plan=explain_plan,
                explain_duration_ms=explain_duration_ms,
                window_minutes=window_minutes,
                warnings=warnings,
            )

        except Exception as e:
            logger.warning(f"Cost estimation failed for {rule.rule_id}: {e}")
            warnings.append(f"EXPLAIN failed: {e}")
            return CostEstimate(window_minutes=window_minutes, warnings=warnings)

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
        if hdx_result.warnings:
            summary["warnings"] = hdx_result.warnings

        return summary
