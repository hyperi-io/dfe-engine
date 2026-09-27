#  Project:      dfe-engine
#  File:         ai/sampling.py
#  Purpose:      Sample landed _json from ClickHouse to feed AI authoring
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Sample a source's landed ``_json`` from ClickHouse to feed the authoring AI.

The VRL generation and query-builder work from EXISTING data - we use ClickHouse
(the ``_json`` column) rather than re-tapping Kafka. This reads sample rows + the
keys present, so the LogParser (VRL generation) and QueryGenerator (query-builder)
have real samples + an available-column list to work from.
"""

from __future__ import annotations

import json
from typing import Any


def sample_json_rows(
    ch: Any, table: str, *, limit: int = 100, source: str | None = None
) -> list[str]:
    """Up to ``limit`` raw ``_json`` values (as JSON strings) from ``table``.

    ``table`` is caller-supplied and must be a TRUSTED, already-qualified identifier
    (e.g. ``\\`db\\`.\\`events\\```) - it is interpolated, not bound (ClickHouse does
    not bind table names). Optionally filter to one source via ``_source``.
    """
    where = ""
    params: dict[str, Any] = {"lim": limit}
    if source is not None:
        where = "WHERE _source = {src:String}"
        params["src"] = source
    rows = ch.query(
        f"SELECT toString(_json) FROM {table} {where} LIMIT {{lim:UInt32}}",
        parameters=params,
    ).result_rows
    return [r[0] for r in rows if r[0]]


def discover_json_keys(samples: list[str], *, max_keys: int = 200) -> list[str]:
    """Top-level keys present across sampled ``_json`` rows (sorted, deduped).

    Parsed in Python - robust across ClickHouse JSON-type quirks. Feeds the
    query-builder's available-columns list and the VRL parser's field discovery.
    """
    keys: set[str] = set()
    for sample in samples:
        try:
            obj = json.loads(sample)
        except ValueError, TypeError:
            continue
        if isinstance(obj, dict):
            keys.update(str(k) for k in obj)
            if len(keys) >= max_keys:
                break
    return sorted(keys)[:max_keys]
