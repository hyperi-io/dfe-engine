#  Project:      dfe-engine
#  File:         src/dfe_engine/cel/syntax.py
#  Purpose:      Fast CEL syntax check + tier classification (one-shot for UI)
#  Language:     Python
#
#  License:      FSL-1.1-ALv2
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""One-shot CEL syntax check for UI live validation.

Designed for the TS UI use case: user types a CEL expression in a text
field, the UI POSTs the string, and gets back a structured result with:

- Whether the syntax is valid (via real CEL parser from hyperi-pylib)
- Profile violations (DFE-disallowed functions)
- The performance tier (Tier 1 / 2 / 3) — unique to dfe-engine
- Referenced fields (for Tier 2/3)
- Recognised operation (for Tier 1) — useful for showing a human-friendly
  description of what the filter will do
- Suggested config changes if the expression needs an opt-in tier

## Architecture

This module layers THREE independent checks:

1. **Syntax** — delegated to ``hyperi_pylib.expression.validate()`` which
   wraps the real ``common-expression-language`` package (same Rust
   ``cel-interpreter`` v0.10.0 that rustlib uses at runtime). Zero drift
   between UI validation and runtime behaviour.

2. **DFE profile** — also from ``hyperi_pylib.expression`` — rejects
   disallowed functions (regex, iteration, time) when the profile is
   strict.

3. **Tier classification** — unique to dfe-engine, mirrors rustlib's
   ``src/transport/filter/classify.rs`` byte-for-byte. Classifies
   expressions into performance tiers for transport filter gating.

All checks are fast (<1ms) — suitable for live validation as the user
types in the UI.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from hyperi_pylib.expression import validate as pylib_validate

from dfe_engine.cel.classify import (
    FilterTier,
    Tier1Op,
    classify_expression,
)


@dataclass
class SyntaxCheckResult:
    """Result of a one-shot CEL syntax check.

    Attributes:
        valid: True if the expression is syntactically valid CEL AND passes
            the DFE profile check.
        errors: List of error messages (syntax errors + profile violations).
        tier: Performance tier (only set if valid=True).
        fields: Fields referenced by the expression (Tier 2/3 only).
        op_kind: Recognised Tier 1 operation kind (Tier 1 only).
        op_field: Field name in the Tier 1 operation (Tier 1 only).
        op_value: Comparison value in the Tier 1 operation (Tier 1 only).
        description: Human-readable description of what the filter does.
        opt_in_required: Config key the operator needs to enable (if any).
    """

    valid: bool
    errors: list[str] = field(default_factory=list)
    tier: FilterTier | None = None
    fields: list[str] = field(default_factory=list)
    op_kind: str | None = None
    op_field: str | None = None
    op_value: str | None = None
    description: str | None = None
    opt_in_required: str | None = None


def check_syntax(
    expression: str,
    *,
    check_profile: bool = False,
) -> SyntaxCheckResult:
    """Perform a one-shot syntax check + tier classification on a CEL expression.

    This is the recommended entry point for UI live validation. Returns a
    `SyntaxCheckResult` with everything the UI needs to render feedback —
    syntax errors, tier badge, field references, plain-English description,
    and any opt-in required.

    Args:
        expression: CEL expression string to check.
        check_profile: If True, also apply DFE profile restrictions
            (reject regex/iteration/time functions). Default False because
            for transport filters we want to classify Tier 3 expressions,
            not reject them.

    Examples:
        >>> r = check_syntax("has(_table)")
        >>> r.valid
        True
        >>> r.tier
        <FilterTier.TIER1: 'tier1'>
        >>> r.description
        "matches messages where field '_table' exists"

        >>> r = check_syntax("")
        >>> r.valid
        False

        >>> r = check_syntax('severity > 3 && source != "internal"')
        >>> r.tier
        <FilterTier.TIER2: 'tier2'>
        >>> r.opt_in_required
        'expression.allow_cel_filters_in (or _out)'
    """
    # Step 1: Tier classification via text pattern matching (fast, matches rustlib)
    try:
        classification = classify_expression(expression)
    except ValueError as e:
        return SyntaxCheckResult(valid=False, errors=[str(e)])

    # Step 2: For Tier 2/3 (expressions that actually run through the CEL engine
    # at runtime), validate syntax via the real CEL parser. Tier 1 expressions
    # bypass the CEL engine entirely in rustlib, so we skip the strict CEL
    # parser check for them — rustlib accepts `has(bareword)` even though real
    # CEL requires `has(qualified.path)`.
    if classification.tier != FilterTier.TIER1:
        pylib_errors = pylib_validate(expression)
        if pylib_errors:
            if not check_profile:
                # Filter out profile violations — we want to classify Tier 3
                # expressions, not reject them.
                profile_errors = [
                    e
                    for e in pylib_errors
                    if "not allowed in the DFE expression profile" in e
                ]
                syntax_errors = [e for e in pylib_errors if e not in profile_errors]
                if syntax_errors:
                    return SyntaxCheckResult(valid=False, errors=syntax_errors)
            else:
                return SyntaxCheckResult(valid=False, errors=pylib_errors)

    result = SyntaxCheckResult(
        valid=True,
        tier=classification.tier,
        fields=classification.fields or [],
    )

    if classification.op is not None:
        result.op_kind = classification.op.kind
        result.op_field = classification.op.field
        result.op_value = classification.op.value
        result.description = _describe_tier1(classification.op)

    if classification.tier == FilterTier.TIER2:
        result.description = _describe_tier2(classification.fields or [])
        result.opt_in_required = "expression.allow_cel_filters_in (or _out)"

    if classification.tier == FilterTier.TIER3:
        result.description = _describe_tier3(classification.fields or [])
        result.opt_in_required = "expression.allow_complex_filters_in (or _out)"

    return result


def _describe_tier1(op: Tier1Op) -> str:
    """Render a human-readable description of a Tier 1 operation."""
    f = op.field
    v = op.value
    if op.kind == "field_exists":
        return f"matches messages where field '{f}' exists"
    if op.kind == "field_not_exists":
        return f"matches messages where field '{f}' does NOT exist"
    if op.kind == "field_equals":
        return f"matches messages where '{f}' == \"{v}\""
    if op.kind == "field_not_equals":
        return f"matches messages where '{f}' != \"{v}\""
    if op.kind == "field_starts_with":
        return f"matches messages where '{f}' starts with \"{v}\""
    if op.kind == "field_ends_with":
        return f"matches messages where '{f}' ends with \"{v}\""
    if op.kind == "field_contains":
        return f"matches messages where '{f}' contains \"{v}\""
    return "Tier 1 SIMD operation"


def _describe_tier2(fields: list[str]) -> str:
    if not fields:
        return "compound CEL expression (Tier 2 — requires CEL engine)"
    return f"compound CEL on {', '.join(fields)} (Tier 2 — requires CEL engine)"


def _describe_tier3(fields: list[str]) -> str:
    if not fields:
        return "complex CEL expression with regex/iteration/time (Tier 3)"
    return f"complex CEL on {', '.join(fields)} (Tier 3 — regex/iteration/time)"
