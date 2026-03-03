"""DFE Column Expression Validator + Builder.

Parses and validates the ``expr`` field on SchemaColumn definitions.
The expr field carries directives that tell the **loader** how to
populate each column.

Directives:
    @source: field              Extract from source data field
    @source: field | fallback   Extract with fallback expression
    @source: first(a/b/c)      First match from multiple candidate fields
    @generated: expr            ClickHouse generates via DEFAULT — loader omits
    @captured: what             Captured from raw payload before transforms
    @captured: what as TYPE     Captured and cast to type
    @computed: expr             Computed from other fields during data prep
    @config: path               Mapping is configurable at runtime

Usage:
    from dfe_engine.source.expression import ExpressionValidator, ExpressionBuilder

    # Validate
    result = ExpressionValidator.validate("@source: timestamp | now()")
    assert result.valid
    assert result.directive == "source"
    assert result.field == "timestamp"
    assert result.fallback == "now()"

    # Build
    expr = ExpressionBuilder.source("timestamp", fallback="now()")
    assert expr == "@source: timestamp | now()"

See dfe-schemas README §DFE Expressions and
dfe-loader DDL-EXPRESSION.md for the full expression language reference.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Sequence


# ---------------------------------------------------------------------------
# Directive names
# ---------------------------------------------------------------------------

DIRECTIVES = frozenset({"source", "generated", "captured", "computed", "config"})

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# Top-level: @directive: body
_DIRECTIVE_RE = re.compile(
    r"^\s*@(?P<directive>[a-z]+)\s*:\s*(?P<body>.+)$",
    re.IGNORECASE | re.DOTALL,
)

# @source with pipe fallback: field | fallback
_SOURCE_PIPE_RE = re.compile(
    r"^(?P<field>[^|]+?)\s*\|\s*(?P<fallback>.+)$",
    re.DOTALL,
)

# @source with first(): first(a/b/c)
_SOURCE_FIRST_RE = re.compile(
    r"^first\(\s*(?P<fields>[^)]+)\s*\)$",
    re.IGNORECASE,
)

# @captured with cast: thing as TYPE
_CAPTURED_AS_RE = re.compile(
    r"^(?P<what>.+?)\s+as\s+(?P<cast_type>\w+)$",
    re.IGNORECASE,
)

# @source nested field: field.nested.path
_DOTTED_FIELD_RE = re.compile(r"^[\w][\w.]*$")

# ClickHouse function call: func(...) or func()
_FUNCTION_CALL_RE = re.compile(r"^\w+\s*\(.*\)$", re.DOTALL)


# ---------------------------------------------------------------------------
# Validation result
# ---------------------------------------------------------------------------


@dataclass
class ExprValidationResult:
    """Result of validating a DFE expression."""

    valid: bool = True
    directive: str | None = None
    body: str = ""
    errors: list[str] = field(default_factory=list)

    # Source-specific
    source_field: str | None = None
    fallback: str | None = None
    candidate_fields: list[str] = field(default_factory=list)

    # Captured-specific
    captured_what: str | None = None
    cast_type: str | None = None

    # Generated/computed
    expression: str | None = None

    # Config
    config_path: str | None = None

    @property
    def field(self) -> str | None:
        """Alias for source_field — the primary field name."""
        return self.source_field

    @property
    def directive_type(self) -> str | None:
        """Alias for directive for API consistency."""
        return self.directive


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------


class ExpressionValidator:
    """Validate DFE column expressions.

    Parses the ``@directive: body`` syntax and validates that
    the expression follows the expected format for each directive.
    """

    @staticmethod
    def validate(expr: str) -> ExprValidationResult:
        """Validate a DFE expression string.

        Args:
            expr: The expression string (e.g. ``@source: timestamp | now()``).

        Returns:
            ExprValidationResult with parsed fields and any errors.
        """
        if not expr or not expr.strip():
            return ExprValidationResult(
                valid=False,
                errors=["Expression is empty"],
            )

        expr = expr.strip()
        m = _DIRECTIVE_RE.match(expr)
        if not m:
            return ExprValidationResult(
                valid=False,
                errors=[
                    f"Expression must start with @directive: — "
                    f"got: {expr[:60]!r}"
                ],
            )

        directive = m.group("directive").lower()
        body = m.group("body").strip()

        if directive not in DIRECTIVES:
            return ExprValidationResult(
                valid=False,
                directive=directive,
                body=body,
                errors=[
                    f"Unknown directive '@{directive}'. "
                    f"Valid: {', '.join(sorted(DIRECTIVES))}"
                ],
            )

        result = ExprValidationResult(directive=directive, body=body)

        # Dispatch to directive-specific validation
        _VALIDATORS[directive](body, result)
        return result

    @staticmethod
    def validate_column_expr(
        column_name: str,
        expr: str | None,
    ) -> list[str]:
        """Validate an expression in the context of a column.

        Returns a list of error strings (empty if valid).
        Convenience for schema-load-time validation.
        """
        if expr is None:
            return []
        result = ExpressionValidator.validate(expr)
        if not result.valid:
            return [f"Column '{column_name}': {e}" for e in result.errors]
        return []


# ---------------------------------------------------------------------------
# Directive-specific validators
# ---------------------------------------------------------------------------


def _validate_source(body: str, result: ExprValidationResult) -> None:
    """Validate @source directive body."""
    # Check for first() syntax — including empty first()
    if re.match(r"^first\s*\(\s*\)\s*$", body, re.IGNORECASE):
        result.valid = False
        result.errors.append("first() has no candidate fields")
        return

    first_m = _SOURCE_FIRST_RE.match(body)
    if first_m:
        fields_str = first_m.group("fields")
        candidates = [f.strip() for f in fields_str.split("/") if f.strip()]
        if not candidates:
            result.valid = False
            result.errors.append("first() has no candidate fields")
            return
        result.candidate_fields = candidates
        result.source_field = candidates[0]
        return

    # Check for pipe fallback
    pipe_m = _SOURCE_PIPE_RE.match(body)
    if pipe_m:
        result.source_field = pipe_m.group("field").strip()
        result.fallback = pipe_m.group("fallback").strip()
        if not result.field:
            result.valid = False
            result.errors.append("Field name before '|' is empty")
        if not result.fallback:
            result.valid = False
            result.errors.append("Fallback expression after '|' is empty")
        return

    # Plain field reference (possibly dotted)
    field_name = body.strip()
    if not field_name:
        result.valid = False
        result.errors.append("@source field name is empty")
        return
    result.source_field = field_name


def _validate_generated(body: str, result: ExprValidationResult) -> None:
    """Validate @generated directive body."""
    if not body:
        result.valid = False
        result.errors.append("@generated expression is empty")
        return
    result.expression = body


def _validate_captured(body: str, result: ExprValidationResult) -> None:
    """Validate @captured directive body."""
    as_m = _CAPTURED_AS_RE.match(body)
    if as_m:
        result.captured_what = as_m.group("what").strip()
        result.cast_type = as_m.group("cast_type").strip()
        if not result.captured_what:
            result.valid = False
            result.errors.append("@captured target before 'as' is empty")
        return

    if not body:
        result.valid = False
        result.errors.append("@captured expression is empty")
        return
    result.captured_what = body


def _validate_computed(body: str, result: ExprValidationResult) -> None:
    """Validate @computed directive body."""
    if not body:
        result.valid = False
        result.errors.append("@computed expression is empty")
        return
    result.expression = body


def _validate_config(body: str, result: ExprValidationResult) -> None:
    """Validate @config directive body."""
    if not body:
        result.valid = False
        result.errors.append("@config path is empty")
        return
    result.config_path = body


_VALIDATORS = {
    "source": _validate_source,
    "generated": _validate_generated,
    "captured": _validate_captured,
    "computed": _validate_computed,
    "config": _validate_config,
}


# ---------------------------------------------------------------------------
# Builder — programmatic expression construction
# ---------------------------------------------------------------------------


class ExpressionBuilder:
    """Build DFE expressions programmatically.

    Provides type-safe construction of expression strings so the UI
    and API consumers don't need to format strings manually.
    """

    @staticmethod
    def source(
        field_name: str,
        *,
        fallback: str | None = None,
    ) -> str:
        """Build a @source expression.

        Args:
            field_name: Source data field name.
            fallback: Optional fallback expression (after ``|``).

        Returns:
            Expression string (e.g. ``@source: timestamp | now()``).
        """
        if fallback:
            return f"@source: {field_name} | {fallback}"
        return f"@source: {field_name}"

    @staticmethod
    def source_first(fields: Sequence[str]) -> str:
        """Build a @source: first(a/b/c) expression.

        Args:
            fields: Candidate field names in priority order.

        Returns:
            Expression string (e.g. ``@source: first(tags/_tags/meta)``).
        """
        return f"@source: first({'/'.join(fields)})"

    @staticmethod
    def generated(expression: str) -> str:
        """Build a @generated expression.

        Args:
            expression: ClickHouse DEFAULT expression.

        Returns:
            Expression string (e.g. ``@generated: now64(3)``).
        """
        return f"@generated: {expression}"

    @staticmethod
    def captured(
        what: str,
        *,
        cast_type: str | None = None,
    ) -> str:
        """Build a @captured expression.

        Args:
            what: What to capture (e.g. ``raw_payload``).
            cast_type: Optional cast type (e.g. ``JSON``).

        Returns:
            Expression string (e.g. ``@captured: raw_payload as JSON``).
        """
        if cast_type:
            return f"@captured: {what} as {cast_type}"
        return f"@captured: {what}"

    @staticmethod
    def computed(expression: str) -> str:
        """Build a @computed expression.

        Args:
            expression: Computation expression.

        Returns:
            Expression string (e.g. ``@computed: geoip(client_ip).country``).
        """
        return f"@computed: {expression}"

    @staticmethod
    def config(path: str) -> str:
        """Build a @config expression.

        Args:
            path: Configuration path.

        Returns:
            Expression string (e.g. ``@config: routing.org_id_field``).
        """
        return f"@config: {path}"


# ---------------------------------------------------------------------------
# Autocomplete data — for UI typeahead support
# ---------------------------------------------------------------------------


def list_directive_types() -> list[dict[str, str]]:
    """List all valid directive types with descriptions.

    Returns:
        List of dicts with ``name`` and ``description`` keys.
    """
    return [
        {
            "name": "source",
            "description": "Extract field from source data",
            "syntax": "@source: field | fallback",
        },
        {
            "name": "generated",
            "description": "ClickHouse generates via DEFAULT — loader omits",
            "syntax": "@generated: expression",
        },
        {
            "name": "captured",
            "description": "Captured from raw payload before transforms",
            "syntax": "@captured: what [as TYPE]",
        },
        {
            "name": "computed",
            "description": "Computed from other fields during data prep",
            "syntax": "@computed: expression",
        },
        {
            "name": "config",
            "description": "Mapping is configurable at runtime",
            "syntax": "@config: path",
        },
    ]
