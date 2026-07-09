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

from __future__ import annotations

import re
from typing import Any

# Default time-field names a HyperDX time filter is likely to sit on.
_DEFAULT_TIME_FIELDS = (
    "timestamp",
    "_timestamp",
    "timestamp_load",
    "_timestamp_load",
    "ts",
)

# What terminates a WHERE predicate (so we replace just the time bound, not the rest).
_TERM = r"(?=\s+AND\b|\s+OR\b|\s+GROUP\b|\s+ORDER\b|\s+LIMIT\b|\s*\)|\s*$)"
_CMP = r"(?:>=|<=|>|<)"


def strip_hyperdx(query: str, time_fields: list[str] | None = None) -> dict[str, Any]:
    """Replace a HyperDX time-bound predicate with the hunt ``{window}`` placeholder.

    Recognises, on any time field:
      ``<field> BETWEEN a AND b``
      ``<field> <op> a AND <field> <op> b`` (a paired range, either bound order)
    Returns ``{"query", "replaced", "needs_window"}``. If no time bound is found the
    query is returned unchanged with ``needs_window=True`` (the UI inserts
    ``{window}``). First cut - refine against real HyperDX queries.
    """
    fields = time_fields or list(_DEFAULT_TIME_FIELDS)
    out = query
    replaced = False
    for field in fields:
        fe = re.escape(field)
        paired = re.compile(
            rf"\b{fe}\s*{_CMP}\s*.+?\s+AND\s+{fe}\s*{_CMP}\s*.+?{_TERM}",
            re.IGNORECASE | re.DOTALL,
        )
        between = re.compile(
            rf"\b{fe}\s+BETWEEN\s+.+?\s+AND\s+.+?{_TERM}", re.IGNORECASE | re.DOTALL
        )
        match = paired.search(out) or between.search(out)
        if match:
            out = out[: match.start()] + "{window}" + out[match.end() :]
            replaced = True
            break
    return {
        "query": re.sub(r"\s+", " ", out).strip(),
        "replaced": replaced,
        "needs_window": not replaced,
    }


def build_query_scaffold(
    columns: list[str], table: str, *, source: str | None = None, limit: int = 100
) -> str:
    """A starter SELECT over the discovered columns, with the hunt ``{window}``.

    ``columns`` are field names (source meta-schema + landed ``_json`` keys); ``table``
    is the source's landed table. The scaffold filters to the source (if given) and
    the hunt ``{window}`` - a starting point for the author to refine, not executed.
    """
    select = ", ".join(columns) if columns else "*"
    predicates = []
    if source:
        predicates.append(f"_source = '{source}'")
    predicates.append("{window}")
    where = " AND ".join(predicates)
    return f"SELECT {select}\nFROM {table}\nWHERE {where}\nLIMIT {limit}"
