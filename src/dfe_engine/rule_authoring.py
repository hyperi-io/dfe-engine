#  Project:      dfe-engine
#  File:         rule_authoring.py
#  Purpose:      Rule-authoring helpers - HyperDX->rule + query scaffolding
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Helpers that turn an exploration into a hunt rule.

WF1 (HyperDX -> rule): strip a HyperDX query's explicit time-bound predicate and
replace it with the hunt ``{window}`` placeholder (the runner substitutes the
incremental predicate there). WF2 (query-builder): scaffold a SELECT over a source's
discovered columns (source meta-schema + landed ``_json`` keys, from ai.sampling).
"""

import re
from typing import Any

from dfe_engine.clickhouse.quoting import quote_literal
from dfe_engine.hunts.hdx_sanitizer import HdxSanitizeError, split_time_window

# Default time-field names a HyperDX time filter is likely to sit on.
_DEFAULT_TIME_FIELDS = (
    "timestamp",
    "_timestamp",
    "timestamp_load",
    "_timestamp_load",
    "ts",
)

_WINDOW = "{window}"


def strip_hyperdx(query: str, time_fields: list[str] | None = None) -> dict[str, Any]:
    """Replace a query's time bounds with the hunt ``{window}`` placeholder.

    The query is read by the same parser as the HyperDX sanitizer. A time bound is
    an AND-conjunct of the filter that compares a time field with a constant --
    ``_timestamp >= toDateTime('2026-01-01')``, ``timestamp BETWEEN 1 AND 2`` -- or
    any column with the epoch instants HyperDX renders. Every such bound is
    removed and ``{window}`` leads the filter; the rest of the query is kept.

    A bound under OR, NOT or a function call cannot come out without changing
    what the query matches, so it stays and is named in ``warnings``.

    Args:
        query: The SELECT to convert.
        time_fields: Time columns whose constant bounds are the window; defaults
            to the DFE timestamp columns. Case-insensitive.

    Returns:
        ``{"query", "replaced", "needs_window", "warnings"}``. With nothing
        replaced the query comes back whitespace-normalised, and ``needs_window``
        says whether it still lacks ``{window}`` for the UI to insert.
    """
    unchanged = re.sub(r"\s+", " ", query).strip()
    try:
        split = split_time_window(query, time_fields or _DEFAULT_TIME_FIELDS, inline_aliases=False)
    except HdxSanitizeError as exc:
        return _stripped(unchanged, [f"Could not read the query: {exc}"])

    warnings = [
        f"A time bound sits under OR, NOT or a function call and stays in the query: {bound}"
        for bound in split.stuck
    ]
    if not split.removed:
        return _stripped(unchanged, warnings)

    where = _WINDOW if split.filter is None else f"{_WINDOW} AND {split.filter}"
    parts = (split.before_filter, "WHERE", where, split.after_filter)
    return {
        "query": " ".join(part for part in parts if part),
        "replaced": True,
        "needs_window": False,
        "warnings": warnings,
    }


def _stripped(query: str, warnings: list[str]) -> dict[str, Any]:
    """The result for a query whose time bounds were left where they were."""
    return {
        "query": query,
        "replaced": False,
        "needs_window": _WINDOW not in query,
        "warnings": warnings,
    }


def build_query_scaffold(
    columns: list[str], table: str, *, source: str | None = None, limit: int = 100
) -> str:
    """A starter SELECT over the discovered columns, with the hunt ``{window}``.

    ``columns`` are field names (source meta-schema + landed ``_json`` keys); ``table``
    is the source's landed table. The scaffold filters to the source (if given) and
    the hunt ``{window}`` - a starting point for the author to refine, not executed.
    The source name is quoted as a literal, so a name holding a quote still
    scaffolds SQL the author can run.
    """
    select = ", ".join(columns) if columns else "*"
    predicates = []
    if source:
        predicates.append(f"_source = {quote_literal(source)}")
    predicates.append("{window}")
    where = " AND ".join(predicates)
    return f"SELECT {select}\nFROM {table}\nWHERE {where}\nLIMIT {limit}"
