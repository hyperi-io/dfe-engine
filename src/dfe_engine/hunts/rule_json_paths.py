#  Project:      dfe-engine
#  File:         hunts/rule_json_paths.py
#  Purpose:      Find the _json paths a rule's WHERE reads, and the ones recent data never carried
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The ``_json`` paths a rule reads, set against the paths its source's recent rows hold.

A path inside ``_json`` is valid SQL whether or not any row carries it, so ClickHouse
plans a misspelt one and the rule matches nothing for ever. The paths the recent rows
carry are the only evidence of a misspelling, so a path outside them is a warning and
never a refusal.
"""

from collections.abc import Collection, Sequence

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from dfe_engine.clickhouse.quoting import quote_identifier
from dfe_engine.hunts.hunt_output import tenant_condition
from dfe_engine.services.schema.json_promotion_service import JSON_COLUMN

# Rows the path read looks at, so the read is bounded however busy the source is.
PATH_SAMPLE_ROWS = 10_000

# Paths named in a warning before the rest are counted.
_NAMED_PATHS = 5


def referenced_json_paths(where: str) -> list[str]:
    """Each distinct ``_json`` path the WHERE reads, in the order it first reads it.

    Args:
        where: The rule's detection WHERE.

    Returns:
        Dotted paths without the ``_json`` prefix. Empty when the text does not parse.
    """
    try:
        tree = sqlglot.parse_one(f"SELECT 1 WHERE {where}", read="clickhouse")
    except SqlglotError, RecursionError:
        return []
    paths: dict[str, None] = {}
    for column in tree.find_all(exp.Column):
        parts = [part.name for part in column.parts]
        if len(parts) < 2 or parts[0] != JSON_COLUMN:
            continue
        # sqlglot keeps three qualifiers on a Column and nests every further segment as a Dot.
        node: exp.Expr = column
        while isinstance(node.parent, exp.Dot) and node.parent.this is node:
            parts.append(node.parent.expression.name)
            node = node.parent
        paths[".".join(parts[1:])] = None
    return list(paths)


def seen_paths_sql(
    source_db: str,
    source_table: str,
    time_column: str,
    window_minutes: int,
    *,
    tenant_ids: Sequence[str] | None = None,
) -> str:
    """The read that returns the JSON paths the source's recent rows carry.

    Args:
        source_db: Database of the rule's source table.
        source_table: The rule's source table.
        time_column: The load-time column the hunt runner windows on.
        window_minutes: Lookback window length.
        tenant_ids: The orgs the read is held to, by the tenant ids their rows carry.
            None reads every org.

    Returns:
        One SELECT returning a single array of path names.
    """
    source = f"{quote_identifier(source_db)}.{quote_identifier(source_table)}"
    window = f"{quote_identifier(time_column)} >= now() - INTERVAL {int(window_minutes)} MINUTE"
    fence = f" AND {tenant_condition(tenant_ids)}" if tenant_ids is not None else ""
    column = quote_identifier(JSON_COLUMN)
    return (
        f"SELECT distinctJSONPaths({column}) FROM "  # noqa: S608 - quoted names, an integer window, quoted tenant ids
        f"(SELECT {column} FROM {source} WHERE {window}{fence} LIMIT {PATH_SAMPLE_ROWS})"
    )


def unseen_paths(referenced: Sequence[str], seen: Collection[str]) -> list[str]:
    """The referenced paths no seen path equals or sits under.

    A path naming an object is carried by every path below it, so ``a`` is seen when
    ``a.b`` is.
    """
    return [
        path
        for path in referenced
        if path not in seen and not any(known.startswith(f"{path}.") for known in seen)
    ]


def unseen_warning(paths: Sequence[str], source: str, window_minutes: int) -> str:
    """The warning naming the paths the rule reads and recent rows do not carry."""
    named = ", ".join(f"{JSON_COLUMN}.{path}" for path in paths[:_NAMED_PATHS])
    more = len(paths) - _NAMED_PATHS
    tail = f" and {more} more" if more > 0 else ""
    return (
        f"{named}{tail} did not appear in the last {window_minutes} minutes of {source}, "
        "so the rule may never match. Check the path against a recent event."
    )
