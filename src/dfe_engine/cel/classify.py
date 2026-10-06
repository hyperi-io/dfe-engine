#  Project:      dfe-engine
#  File:         src/dfe_engine/cel/classify.py
#  Purpose:      CEL expression tier classification (Python mirror of Rust classify.rs)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""CEL expression classifier -- mirrors the Rust classify.rs logic.

Classifies a CEL expression into one of three performance tiers:

- **Tier 1** -- SIMD field ops (has, ==, !=, startsWith, endsWith, contains).
  Executed as direct byte operations, no CEL engine.
- **Tier 2** -- Standard CEL (compound logic, size(), numeric comparison).
  Requires ``allow_cel_filters_in/out`` opt-in.
- **Tier 3** -- Complex CEL (regex via matches, iteration via exists/all,
  time functions). Requires ``allow_complex_filters_in/out`` opt-in.

This Python implementation MUST match the Rust classification in
``src/transport/filter/classify.rs`` (hyperi-io/scalo-rs) byte-for-byte
-- divergence means the UI validates differently from the runtime engine.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class FilterTier(str, Enum):
    """Performance tier for a filter expression."""

    TIER1 = "tier1"
    """SIMD field ops (~50-200 ns). Always enabled."""

    TIER2 = "tier2"
    """Standard CEL (~500ns-1us). Requires allow_cel_filters."""

    TIER3 = "tier3"
    """Complex CEL (~5-50us). Requires allow_complex_filters."""


@dataclass
class Tier1Op:
    """Recognised Tier 1 operation extracted from the expression."""

    kind: str  # 'field_exists', 'field_not_exists', 'field_equals', 'field_not_equals', 'field_starts_with', 'field_ends_with', 'field_contains'
    field: str
    value: str | None = None  # for equals/startsWith/endsWith/contains


@dataclass
class ClassifyResult:
    """Result of classifying an expression."""

    tier: FilterTier
    op: Tier1Op | None = None  # only set for Tier 1
    fields: list[str] | None = None  # referenced fields (Tier 2/3)


# ---------------------------------------------------------------------------
# Tier 1 regex patterns -- MUST match src/transport/filter/classify.rs
# ---------------------------------------------------------------------------

_RE_HAS = re.compile(r"^\s*has\(\s*([\w.]+)\s*\)\s*$")
_RE_NOT_HAS = re.compile(r"^\s*!\s*has\(\s*([\w.]+)\s*\)\s*$")
_RE_EQ_STR = re.compile(r'^\s*([\w.]+)\s*==\s*"([^"]*)"\s*$')
_RE_NEQ_STR = re.compile(r'^\s*([\w.]+)\s*!=\s*"([^"]*)"\s*$')
_RE_STARTS_WITH = re.compile(r'^\s*([\w.]+)\s*\.\s*startsWith\(\s*"([^"]*)"\s*\)\s*$')
_RE_ENDS_WITH = re.compile(r'^\s*([\w.]+)\s*\.\s*endsWith\(\s*"([^"]*)"\s*\)\s*$')
_RE_CONTAINS = re.compile(r'^\s*([\w.]+)\s*\.\s*contains\(\s*"([^"]*)"\s*\)\s*$')

# Restricted functions (Tier 3)
_RESTRICTED_FUNCTIONS = (
    "matches",
    "map",
    "filter",
    "exists",
    "all",
    "exists_one",
    "timestamp",
    "duration",
)

# CEL keywords and built-in functions (NOT field references)
_CEL_KEYWORDS = frozenset(
    [
        "true",
        "false",
        "null",
        "in",
        "has",
        "size",
        "int",
        "uint",
        "double",
        "string",
        "bool",
        "type",
        "contains",
        "startsWith",
        "endsWith",
        "matches",
        "map",
        "filter",
        "exists",
        "all",
        "exists_one",
        "timestamp",
        "duration",
    ]
)


def classify_expression(expr: str) -> ClassifyResult:
    """Classify a CEL expression into a performance tier.

    Raises:
        ValueError: if the expression is empty or whitespace-only.

    Examples:
        >>> classify_expression("has(_table)").tier
        <FilterTier.TIER1: 'tier1'>
        >>> classify_expression('severity > 3 && source != "internal"').tier
        <FilterTier.TIER2: 'tier2'>
        >>> classify_expression('field.matches("^prod-.*")').tier
        <FilterTier.TIER3: 'tier3'>
    """
    trimmed = expr.strip()
    if not trimmed:
        raise ValueError("empty expression")

    # Try Tier 1 patterns
    op = _try_tier1(trimmed)
    if op is not None:
        return ClassifyResult(tier=FilterTier.TIER1, op=op)

    # Check for restricted functions (Tier 3) vs standard (Tier 2)
    has_restricted = _check_restricted_functions(trimmed)
    fields = _extract_field_references(trimmed)

    if has_restricted:
        return ClassifyResult(tier=FilterTier.TIER3, fields=fields)
    return ClassifyResult(tier=FilterTier.TIER2, fields=fields)


def _try_tier1(expr: str) -> Tier1Op | None:
    """Try to match a Tier 1 pattern. Returns None if no pattern matches."""
    m = _RE_HAS.match(expr)
    if m:
        return Tier1Op(kind="field_exists", field=m.group(1))

    m = _RE_NOT_HAS.match(expr)
    if m:
        return Tier1Op(kind="field_not_exists", field=m.group(1))

    m = _RE_EQ_STR.match(expr)
    if m:
        return Tier1Op(kind="field_equals", field=m.group(1), value=m.group(2))

    m = _RE_NEQ_STR.match(expr)
    if m:
        return Tier1Op(kind="field_not_equals", field=m.group(1), value=m.group(2))

    m = _RE_STARTS_WITH.match(expr)
    if m:
        return Tier1Op(kind="field_starts_with", field=m.group(1), value=m.group(2))

    m = _RE_ENDS_WITH.match(expr)
    if m:
        return Tier1Op(kind="field_ends_with", field=m.group(1), value=m.group(2))

    m = _RE_CONTAINS.match(expr)
    if m:
        return Tier1Op(kind="field_contains", field=m.group(1), value=m.group(2))

    return None


def _check_restricted_functions(expr: str) -> bool:
    """Check if the expression uses any restricted functions (Tier 3).

    Scans for function names followed by `(`, skipping occurrences inside
    string literals.
    """
    for func in _RESTRICTED_FUNCTIONS:
        pattern = f"{func}("
        pos = expr.find(pattern)
        if pos != -1:
            # Check we're not inside a string literal by counting quotes before pos
            before = expr[:pos]
            quote_count = before.count('"')
            if quote_count % 2 == 0:
                # Even number of quotes = we're outside a string
                return True
    return False


def _extract_field_references(expr: str) -> list[str]:
    """Extract field references from an expression for Tier 2/3.

    Scans for identifier patterns that aren't CEL keywords or function names.
    For method calls like `field.matches("...")`, extracts only the receiver.
    """
    # Match dotted identifier (potentially nested)
    ident_re = re.compile(r"[a-zA-Z_][\w.]*")

    # Build a mask of positions inside string literals
    in_string_mask = [False] * len(expr)
    in_string = False
    prev_was_escape = False
    for i, ch in enumerate(expr):
        if in_string:
            in_string_mask[i] = True
        if ch == '"' and not prev_was_escape:
            in_string = not in_string
        prev_was_escape = ch == "\\" and not prev_was_escape

    fields: list[str] = []
    for m in ident_re.finditer(expr):
        if in_string_mask[m.start()]:
            continue

        ident = m.group(0)
        # If followed by '(', it's a function call -- strip the last segment
        after = expr[m.end() :].lstrip()
        if after.startswith("("):
            if "." in ident:
                # Method call on a field -- keep the receiver
                ident = ident.rsplit(".", 1)[0]
            else:
                # Bare function call (e.g., has(), size()) -- not a field
                continue

        if not ident:
            continue

        # Skip CEL keywords (check the leading segment)
        base = ident.split(".")[0]
        if base in _CEL_KEYWORDS:
            continue

        if ident not in fields:
            fields.append(ident)

    return fields
