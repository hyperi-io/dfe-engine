#  Project:      dfe-engine
#  File:         hyperdx/sources.py
#  Purpose:      One HyperDX source per deployed DFE source, on every team
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""A HyperDX source per DFE source, so a deploy is searchable without a UI step.

The fork seeds a team's fixed set on first contact (``main``, ``hunts``, and the
otel/system sources on the platform team) from
``dfe/controllers/org-connection.ts``. Nothing seeded a source for a DFE source an
operator adds later, so its table existed in ClickHouse and was invisible in
HyperDX until someone created the source by hand.

The engine authenticates as ``svc:dfe-engine``, which the fork resolves to the
team named by ``DFE_AUTH_DEFAULT_TEAM``. No human is in that team and it holds no
connection, so the team-scoped ``/sources`` surface is the wrong place to write:
the source would land where nobody can read it. The fork's ``/dfe/sources``
routes fan the write across every team instead, each over that team's own
connection, and tenant rows stay fenced by the ClickHouse row policies behind it.

A deploy calls :func:`ensure_source`, a delete calls :func:`remove_source`, and
:func:`list_sources_by_team` reads back where a source actually landed. All three
are non-fatal: the client returns None on failure, and a HyperDX outage must
never fail a source deploy.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from scalo.logger import logger

from dfe_engine.hyperdx.client import HyperDXClient

__all__ = [
    "ensure_source",
    "list_sources_by_team",
    "remove_source",
    "source_spec",
    "timestamp_column",
]

# First match wins: the event time where the header profile carries one, the
# insertion time on `passthrough`, which has no `_timestamp` column at all.
_TIMESTAMP_COLUMNS = ("_timestamp", "_timestamp_load")

# The structured payload column, in every core header profile. `_raw` is the
# exception (captured only when asked for) and may be NULL, so it is never the
# body, the implicit search column or the default view.
_PAYLOAD_COLUMN = "_json"


def timestamp_column(columns: Iterable[str]) -> str | None:
    """Return the column a HyperDX source should read as its event time.

    Args:
        columns: Column names on the deployed table.

    Returns:
        The first of ``_timestamp``, ``_timestamp_load`` present, else None --
        the fork rejects a source whose timestamp expression is empty.
    """
    present = set(columns)
    for candidate in _TIMESTAMP_COLUMNS:
        if candidate in present:
            return candidate
    return None


def source_spec(*, database: str, table: str, timestamp: str) -> dict[str, Any]:
    """Build the fork source body for one DFE source.

    The name is the path segment and the connection is resolved per team, so
    neither appears here. Every other field mirrors the seeded ``main`` source,
    so a per-source view behaves like the landing view it was cut from.

    Args:
        database: ClickHouse database the table lives in.
        table: ClickHouse table name.
        timestamp: Column to read as the event time.

    Returns:
        The request body for ``PUT /dfe/sources/{name}``.
    """
    return {
        "kind": "log",
        "from": {"databaseName": database, "tableName": table},
        "timestampValueExpression": timestamp,
        "displayedTimestampValueExpression": timestamp,
        "implicitColumnExpression": _PAYLOAD_COLUMN,
        "bodyExpression": _PAYLOAD_COLUMN,
        "defaultTableSelectExpression": f"{timestamp},{_PAYLOAD_COLUMN}",
    }


async def ensure_source(
    client: HyperDXClient,
    *,
    name: str,
    database: str,
    table: str,
    columns: Iterable[str],
) -> list[str] | None:
    """Create or update the HyperDX source for one DFE source, on every team.

    Args:
        client: HyperDX client.
        name: DFE source name; the HyperDX source carries the same name.
        database: ClickHouse database the table was deployed into.
        table: ClickHouse table name.
        columns: Column names on the deployed table.

    Returns:
        The teams the source now sits on, or None when nothing was written
        (HyperDX unreachable, no timestamp column, or the name belongs to the
        fork's own seeded set).
    """
    timestamp = timestamp_column(columns)
    if timestamp is None:
        logger.warning("No timestamp column for a HyperDX source", source=name)
        return None

    spec = source_spec(database=database, table=table, timestamp=timestamp)
    result = await client.put_dfe_source(name, spec)
    if result is None:
        return None

    written = [str(team) for team in result.get("written") or []]
    skipped = [str(team) for team in result.get("skipped") or []]
    logger.info(
        "HyperDX source written",
        source=name,
        teams=len(written),
        skipped=len(skipped),
    )
    return written


async def remove_source(client: HyperDXClient, *, name: str) -> bool:
    """Delete the HyperDX source for a DFE source that is going away.

    Args:
        client: HyperDX client.
        name: DFE source name.

    Returns:
        True when the source is gone from every team (deleted, or never there),
        False when HyperDX could not be reached or refused the delete.
    """
    result = await client.delete_dfe_source(name)
    if result is None:
        return False
    removed = [str(team) for team in result.get("removed") or []]
    logger.info("HyperDX source removed", source=name, teams=len(removed))
    return True


async def list_sources_by_team(client: HyperDXClient) -> list[dict[str, Any]] | None:
    """Read back which teams hold which DFE sources.

    Returns:
        One entry per team, or None when HyperDX could not be reached.
    """
    result = await client.list_dfe_sources()
    if result is None:
        return None
    teams = result.get("teams")
    return teams if isinstance(teams, list) else []
