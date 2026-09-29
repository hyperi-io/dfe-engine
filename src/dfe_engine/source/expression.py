"""DFE Column Expression Validator + Builder.

Parses and validates the ``expr`` field on SchemaColumn definitions. dfe-loader
acts on six directives (``DIRECTIVE_NAMES`` in its ``src/column_meta/mod.rs``):
``@skip``, ``@default``, ``@source``, ``@renamed``, ``@computed`` and ``@coerce``.
This validator accepts ``@source`` and ``@computed``, plus two descriptive
labels, ``@generated`` and ``@config``, which the loader drops as unknown
directives because what they describe comes from elsewhere.

Directives:
    @source: field              Extract from source data field
    @source: field | fallback   Extract with fallback expression
    @source: first(a/b/c)       First match from multiple candidate fields
    @computed: expr             Computed from other fields during data prep
    @generated: expr            Descriptive: the column's DEFAULT clause fills it
    @config: path               Descriptive: the mapping is configurable at runtime

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

See dfe-loader docs/clickhouse/DDL-DIRECTIVES.md for the full expression
language reference.
"""

import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Directive names
# ---------------------------------------------------------------------------

DIRECTIVES = frozenset({"source", "computed", "generated", "config"})

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

    # Generated/computed
    expression: str | None = None

    # Config
    config_path: str | None = None

    @property
    def field(self) -> str | None:
        """Alias for source_field -- the primary field name."""
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
                errors=[f"Expression must start with @directive: -- got: {expr[:60]!r}"],
            )

        directive = m.group("directive").lower()
        body = m.group("body").strip()

        if directive not in DIRECTIVES:
            return ExprValidationResult(
                valid=False,
                directive=directive,
                body=body,
                errors=[
                    f"Unknown directive '@{directive}'. Valid: {', '.join(sorted(DIRECTIVES))}"
                ],
            )

        result = ExprValidationResult(directive=directive, body=body)

        # Dispatch to directive-specific validation
        _VALIDATORS[directive](body, result)
        return result


# ---------------------------------------------------------------------------
# Directive-specific validators
# ---------------------------------------------------------------------------


def _validate_source(body: str, result: ExprValidationResult) -> None:
    """Validate @source directive body."""
    # Check for first() syntax -- including empty first()
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
    "computed": _validate_computed,
    "generated": _validate_generated,
    "config": _validate_config,
}


# ---------------------------------------------------------------------------
# Builder -- programmatic expression construction
# ---------------------------------------------------------------------------


class ExpressionBuilder:
    """Build DFE expressions, so a caller never formats the directive by hand."""

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
