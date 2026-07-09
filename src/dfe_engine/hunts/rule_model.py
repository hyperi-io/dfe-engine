"""Hunt Rule model — Pydantic model for detection rule CRUD.

A Rule represents a user-defined detection query that scans a source
table and writes lean results (matched_uuid + rule metadata) to the
hunt results table.

Rules can be created two ways:
1. From user-supplied SQL (typically from HyperDX) via the RuleRewriter
2. From a CEL expression via transpile_to_clickhouse() for query pushdown

Both approaches can be combined: SQL provides structure, CEL adds
an additional pushdown filter ANDed into the WHERE clause.

Usage:
    from dfe_engine.hunts.rule_model import Rule, RuleCreate

    # Create from user SQL
    rule = Rule.from_create(
        RuleCreate(
            name="Windows Privilege Escalation",
            severity="high",
            user_sql="SELECT * FROM acme.windows_audit WHERE process_name = 'certutil.exe'",
        ),
        rule_id="win_priv_esc_01",
    )

    # Create from CEL expression (query pushdown)
    rule = Rule.from_create(
        RuleCreate(
            name="High-Value Transfers",
            severity="critical",
            cel_filter='severity == "critical" && amount > 10000',
            source="payment_events",
        ),
        rule_id="high_value_01",
    )
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from .rule_rewriter import ParsedRule, RuleRewriter


class RuleCreate(BaseModel):
    """Input model for creating a new detection rule.

    At least one of ``user_sql`` or ``cel_filter`` must be provided.
    When both are present, the CEL filter is transpiled to SQL and
    ANDed with the detection logic parsed from user_sql.
    """

    name: str = Field(..., description="Human-readable rule name")
    severity: str = Field(
        default="medium",
        description="Detection severity (low, medium, high, critical)",
    )
    user_sql: str | None = Field(
        default=None,
        description="User-supplied SQL SELECT statement",
    )
    cel_filter: str | None = Field(
        default=None,
        description="CEL expression for query pushdown (transpiled to SQL WHERE)",
    )
    hunt_name: str | None = Field(
        default=None,
        description="Parent hunt name (optional — may be assigned later)",
    )
    source: str | None = Field(
        default=None,
        description="Source name for SourceRegistry resolution (optional)",
    )

    @field_validator("severity")
    @classmethod
    def _validate_severity(cls, v: str) -> str:
        valid = {"low", "medium", "high", "critical"}
        v = v.lower()
        if v not in valid:
            raise ValueError(f"Invalid severity '{v}'. Valid: {', '.join(sorted(valid))}")
        return v

    @model_validator(mode="after")
    def _require_sql_or_cel(self) -> RuleCreate:
        if not self.user_sql and not self.cel_filter:
            raise ValueError("At least one of 'user_sql' or 'cel_filter' must be provided.")
        return self

    def parse(self, rewriter: RuleRewriter | None = None) -> ParsedRule:
        """Parse the user SQL using RuleRewriter.

        Returns:
            ParsedRule with extracted components. Returns an empty
            ParsedRule if no user_sql was provided.
        """
        if not self.user_sql:
            return ParsedRule()
        rw = rewriter or RuleRewriter()
        return rw.parse_user_sql(self.user_sql)


class Rule(BaseModel):
    """A detection rule ready for storage and execution.

    Contains the parsed detection logic, metadata, and the original
    user SQL for reference. Rules are immutable once created — to
    modify, create a new version.

    The ``where_clause`` is the final SQL WHERE fragment used in hunt
    queries. It may originate from user SQL parsing, CEL transpilation,
    or both (ANDed together).
    """

    rule_id: str = Field(..., description="Unique rule identifier")
    name: str = Field(..., description="Human-readable rule name")
    severity: str = Field(default="medium", description="Detection severity")
    source_db: str | None = Field(default=None, description="Source database")
    source_table: str | None = Field(default=None, description="Source table name")
    where_clause: str = Field(default="", description="Detection logic (WHERE clause)")
    cel_filter: str | None = Field(
        default=None,
        description="Original CEL expression (before transpilation)",
    )
    had_select_star: bool = Field(default=False, description="Whether user used SELECT *")
    original_sql: str = Field(default="", description="Original user SQL")
    hunt_name: str | None = Field(default=None, description="Parent hunt name")
    source: str | None = Field(default=None, description="Source name for registry")
    warnings: list[str] = Field(default_factory=list, description="Parse warnings")
    created_at: str = Field(
        default_factory=lambda: datetime.now(UTC).isoformat(),
        description="Creation timestamp (ISO 8601)",
    )

    @classmethod
    def from_create(
        cls,
        create: RuleCreate,
        rule_id: str,
        rewriter: RuleRewriter | None = None,
    ) -> Rule:
        """Create a Rule from a RuleCreate input.

        Handles three cases:
        1. SQL only — parses user_sql for detection logic
        2. CEL only — transpiles cel_filter to SQL WHERE clause
        3. Both — parses SQL and ANDs transpiled CEL into where_clause

        Args:
            create: RuleCreate input with SQL and/or CEL filter.
            rule_id: Unique rule identifier.
            rewriter: Optional custom RuleRewriter instance.

        Returns:
            Rule instance ready for storage.

        Raises:
            ExpressionError: If cel_filter is invalid CEL or violates DFE profile.
            TranspileError: If cel_filter cannot be transpiled to SQL.
        """
        parsed = create.parse(rewriter)
        where_clause = parsed.where_clause
        warnings = list(parsed.warnings)

        # Transpile CEL filter to SQL and merge with parsed WHERE
        if create.cel_filter:
            from scalo.expression import transpile_to_clickhouse

            cel_sql = transpile_to_clickhouse(create.cel_filter)

            if where_clause.strip():
                # Both SQL and CEL — AND them together
                where_clause = f"({where_clause.strip()}) AND ({cel_sql})"
            else:
                where_clause = cel_sql

        return cls(
            rule_id=rule_id,
            name=create.name,
            severity=create.severity,
            source_db=parsed.source_db,
            source_table=parsed.source_table,
            where_clause=where_clause,
            cel_filter=create.cel_filter,
            had_select_star=parsed.had_select_star,
            original_sql=parsed.original_sql,
            hunt_name=create.hunt_name,
            source=create.source,
            warnings=warnings,
        )

    def to_rule_info(self) -> dict[str, Any]:
        """Convert to the dict format expected by Hunt.rules.

        Returns a dict compatible with Hunt.__init__(rules=[...]),
        enabling Rule-based hunts to integrate with the existing
        hunt execution pipeline.
        """
        info: dict[str, Any] = {
            "rule_name": self.rule_id,
        }
        if self.source_table:
            info["source_table_name"] = self.source_table
        if self.source:
            info["source"] = self.source
        return info

    def validate_rule(self) -> list[str]:
        """Validate the rule for execution readiness.

        Returns:
            List of error strings (empty if valid).
        """
        errors: list[str] = []

        if not self.where_clause.strip():
            errors.append("Rule has empty detection logic (WHERE clause).")

        if not self.source_table and not self.source:
            errors.append(
                "Rule has no source table or source name — cannot determine what to scan."
            )

        if self.cel_filter:
            from scalo.expression import validate as validate_cel

            cel_errors = validate_cel(self.cel_filter)
            if cel_errors:
                errors.append(f"CEL filter validation failed: {'; '.join(cel_errors)}")

        if self.had_select_star:
            # Warning, not error — the system rewrites to lean output
            pass

        return errors
