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

from typing import Any

# Substrings that mark a param key whose value must never be logged (a floor, not
# a ceiling - extend, never narrow). Masking is for logs ONLY, never execution.
_SENSITIVE = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "access_key",
    "private_key",
    "client_secret",
    "credential",
    "authorization",
)


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


def mask_sensitive(params: dict[str, Any]) -> dict[str, Any]:
    """Redact obviously-sensitive param VALUES for logging (never for execution).

    A key containing any of the sensitive tokens gets ``[HIDDEN]``; everything else
    passes through. Apply at the log boundary, never to a params dict that is about
    to be executed.
    """
    return {
        key: ("[HIDDEN]" if any(token in key.lower() for token in _SENSITIVE) else value)
        for key, value in params.items()
    }
