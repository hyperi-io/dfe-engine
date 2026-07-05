#  Project:      dfe-engine
#  File:         sampling/clickhouse_reader.py
#  Purpose:      Read sample _json lines from ClickHouse for the sampler
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""ClickHouse reads for the sampler.

All reads select ``toString(_json)`` as the first (and only) column so the
result is a list of raw event strings - the shape both the parsed-row path and
logreducer want. ``target`` is interpolated, not bound, because ClickHouse cannot
bind table names: for a registered source it is the engine-rendered
``\\`db\\`.\\`events\\```, and for an explicit override it has ALREADY passed
``service._validate_table_override`` (a bare db.table identifier). ``filter`` is
raw SQL interpolated verbatim - the router gates any caller-supplied
``table``/``filter`` behind ``query:raw`` (NOT the broad ``sampler:read`` that
viewers hold), so this sink is only reachable by a raw-SQL-authorised caller
(F-SAMPLER-SQLI).
"""

from __future__ import annotations

from typing import Any


def _raw_client(ch: Any) -> Any:
    """Unwrap the engine's ClickHouseClientWrapper to the clickhouse-connect Client.

    logreducer's ``ClickHouseSource`` wants the native driver client (block
    streaming); the wrapper stores it on ``_client``. A bare client passes through.
    """
    return getattr(ch, "_client", ch)


def build_where(
    *,
    source_label: str | None,
    filter_sql: str | None,
    since: str | None,
    until: str | None,
    timestamp_field: str,
) -> tuple[str, dict[str, Any]]:
    """Assemble a WHERE clause + bound parameters.

    ``source_label`` filters the shared landing table by ``_source``; leave it
    None when sampling a per-source table (already scoped). Time bounds bind
    server-side; ``filter_sql`` is raw SQL interpolated verbatim, reachable only
    behind the router's ``query:raw`` gate (F-SAMPLER-SQLI).
    """
    clauses: list[str] = []
    params: dict[str, Any] = {}
    if source_label:
        clauses.append("_source = {src:String}")
        params["src"] = source_label
    if since:
        clauses.append(f"{timestamp_field} >= {{since:DateTime64(3)}}")
        params["since"] = since
    if until:
        clauses.append(f"{timestamp_field} <= {{until:DateTime64(3)}}")
        params["until"] = until
    if filter_sql:
        clauses.append(f"({filter_sql})")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return where, params


def read_recent(
    ch: Any,
    target: str,
    *,
    limit: int,
    where: str,
    params: dict[str, Any],
    timestamp_field: str,
    max_execution_time: int,
) -> list[str]:
    """Newest ``limit`` ``_json`` rows, ordered by ``timestamp_field`` DESC."""
    sql = (
        f"SELECT toString(_json) FROM {target} {where} "
        f"ORDER BY {timestamp_field} DESC LIMIT {{lim:UInt64}}"
    )
    return _run(ch, sql, {**params, "lim": limit}, max_execution_time)


def read_random(
    ch: Any,
    target: str,
    *,
    limit: int,
    where: str,
    params: dict[str, Any],
    seed: int | None,
    max_execution_time: int,
) -> list[str]:
    """Random ``limit`` rows.

    Unseeded uses ``ORDER BY rand()``. Seeded is reproducible via a deterministic
    hash-based ordering keyed on the seed: CH's ``rand(x)`` IGNORES x for its RNG
    (x only blocks common-subexpression elimination), so ``rand(seed)`` is NOT
    reproducible. ``cityHash64(toString(_json), seed)`` gives a stable
    pseudo-random permutation for the same data + seed -- a fixed sample per
    seed, not statistical resampling, but it honours the reproducibility
    contract (same approach scan_query already uses for its stable subset).

    This is a full scan of the filtered set (CH has no cheap unseeded reservoir
    without a ``SAMPLE BY`` key), bounded by ``max_execution_time``; for very
    large tables prefer a tighter ``filter`` or time window.
    """
    if seed is not None:
        order = f"cityHash64(toString(_json), {int(seed)})"
    else:
        order = "rand()"
    sql = f"SELECT toString(_json) FROM {target} {where} ORDER BY {order} LIMIT {{lim:UInt64}}"
    return _run(ch, sql, {**params, "lim": limit}, max_execution_time)


def scan_query(
    target: str,
    *,
    where: str,
    scan_rows: int,
) -> str:
    """A stable, bounded SELECT for logreducer to reduce.

    ``ORDER BY cityHash64(_json)`` gives a deterministic pseudo-random subset so
    the reducer's multi-pass modes (dedup -> template -> anomaly) see the SAME
    rows on every pass, which its re-iterable-source contract requires. A bare
    ``LIMIT`` would return different rows per pass.
    """
    return (
        f"SELECT toString(_json) FROM {target} {where} "
        f"ORDER BY cityHash64(toString(_json)) LIMIT {scan_rows}"
    )


def _run(ch: Any, sql: str, params: dict[str, Any], max_execution_time: int) -> list[str]:
    result = ch.query(
        sql,
        parameters=params,
        settings={"max_execution_time": max_execution_time},
    )
    return [r[0] for r in result.result_rows if r[0]]
