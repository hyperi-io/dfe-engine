#  Project:      dfe-engine
#  File:         clickhouse/quoting.py
#  Purpose:      Canonical ClickHouse identifier + literal quoting (injection-safe)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Canonical ClickHouse identifier + literal quoting - ONE injection-safe seam.

Every DDL / SQL builder quotes through here instead of re-deriving the rules (the
CH-sprawl audit found four independent, inconsistent escapers). PREFER server-side
parameter binding (clickhouse-connect ``parameters={name: Type}``) for VALUES on
the read path; use :func:`quote_literal` only where a value must be spliced into
DDL/DML text that has no bind slot (row-policy predicates, meta-table projections).
"""

from __future__ import annotations


def quote_identifier(name: str) -> str:
    """Backtick-quote a CH identifier, escaping embedded backticks.

    For a SINGLE name (org, role, column). A dotted ``db.table`` reference must
    quote each part separately - do not pass it whole (it would become one
    identifier literally named ``db.table``).
    """
    return "`" + name.replace("`", "``") + "`"


def quote_literal(value: str) -> str:
    """Single-quote a CH string literal - escape backslashes THEN single quotes.

    Order is load-bearing: ClickHouse honours C-style backslash escapes, so ``\\``
    MUST be doubled before ``'`` is doubled, else a crafted ``\\' OR 1=1`` breaks
    out of a RESTRICTIVE row-policy predicate and tenant isolation fails open
    (F-ROWPOLICY-BACKSLASH, proven live on CH 25.8). Matches clickhouse-connect's
    ``escape_str``. Prefer server-side ``parameters={}`` binding where a slot exists.
    """
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"
