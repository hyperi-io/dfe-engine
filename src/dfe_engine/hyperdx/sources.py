#  Project:      dfe-engine
#  File:         hyperdx/sources.py
#  Purpose:      One HyperDX source per deployed DFE source, over that source's table
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""A HyperDX source per DFE source, so a deploy is searchable without a UI step.

The fork seeds a team's fixed set on first contact (``default``, ``hunts``, and
the otel/system sources on the platform team) from
``dfe/controllers/org-connection.ts``. Nothing seeded a source for a DFE source an
operator adds later, so its table existed in ClickHouse and was invisible in
HyperDX until someone created the source by hand.

A deploy calls :func:`ensure_source` and a delete calls :func:`remove_source`,
both over the fork's ``/sources`` control surface, which the engine's machine JWT
already reaches. The spec mirrors the seeded ``default`` source exactly -- same
kind, same connection, same column expressions -- so a per-source view behaves
like the landing view it was cut from.

Idempotent: the source is keyed by NAME on the caller's team, so a re-deploy
updates the one that is there rather than adding a second. Non-fatal throughout:
every client call returns None on failure, and a HyperDX outage must never fail a
source deploy.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from scalo.logger import logger

from dfe_engine.hyperdx.client import HyperDXClient

__all__ = [
    "TEMPLATE_SOURCE_NAME",
    "ensure_source",
    "remove_source",
    "source_spec",
    "timestamp_column",
]

# The fork seeds this source over the landing table on EVERY team; its connection
# is the one every other DFE source on that team has to hang off.
TEMPLATE_SOURCE_NAME = "default"

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


def source_spec(
    *,
    name: str,
    database: str,
    table: str,
    connection_id: str,
    timestamp: str,
) -> dict[str, Any]:
    """Build the fork ``SourceSchemaNoId`` body for one DFE source.

    Args:
        name: DFE source name; the HyperDX source carries the same name.
        database: ClickHouse database the table lives in.
        table: ClickHouse table name.
        connection_id: Fork connection ObjectId on the caller's team.
        timestamp: Column to read as the event time.

    Returns:
        The request body for ``POST /sources`` (and, with an ``id``, for ``PUT``).
    """
    return {
        "name": name,
        "kind": "log",
        "connection": connection_id,
        "from": {"databaseName": database, "tableName": table},
        "timestampValueExpression": timestamp,
        "displayedTimestampValueExpression": timestamp,
        "implicitColumnExpression": _PAYLOAD_COLUMN,
        "bodyExpression": _PAYLOAD_COLUMN,
        "defaultTableSelectExpression": f"{timestamp},{_PAYLOAD_COLUMN}",
    }


def _document_id(document: dict[str, Any]) -> str:
    """The fork serialises mongoose docs with virtuals, so ``id`` or ``_id``."""
    return str(document.get("id") or document.get("_id") or "")


async def _connection_id(client: HyperDXClient, sources: Sequence[dict[str, Any]]) -> str:
    """The connection a new source must use: the seeded template's, else the team's.

    Args:
        client: HyperDX client.
        sources: The caller's team sources, already fetched.

    Returns:
        A connection id, or "" when the team holds neither a source nor a
        connection to take one from.
    """
    template = [s for s in sources if s.get("name") == TEMPLATE_SOURCE_NAME]
    for source in [*template, *sources]:
        connection_id = str(source.get("connection") or "")
        if connection_id:
            return connection_id

    for connection in await client.list_connections() or []:
        connection_id = _document_id(connection)
        if connection_id:
            return connection_id
    return ""


async def ensure_source(
    client: HyperDXClient,
    *,
    name: str,
    database: str,
    table: str,
    columns: Iterable[str],
) -> str | None:
    """Create or update the HyperDX source for one DFE source.

    Args:
        client: HyperDX client.
        name: DFE source name.
        database: ClickHouse database the table was deployed into.
        table: ClickHouse table name.
        columns: Column names on the deployed table.

    Returns:
        The HyperDX source id, or None when nothing was written (HyperDX
        unreachable, no connection to hang it on, or no timestamp column).
    """
    existing = await client.list_sources()
    if existing is None:
        return None

    current = next((s for s in existing if s.get("name") == name), None)
    connection_id = str(current.get("connection") or "") if current else ""
    if not connection_id:
        connection_id = await _connection_id(client, existing)
    if not connection_id:
        logger.warning("No HyperDX connection to hang the source on", source=name)
        return None

    timestamp = timestamp_column(columns)
    if timestamp is None:
        logger.warning("No timestamp column for a HyperDX source", source=name)
        return None

    spec = source_spec(
        name=name,
        database=database,
        table=table,
        connection_id=connection_id,
        timestamp=timestamp,
    )

    if current is not None:
        source_id = _document_id(current)
        if not source_id:
            logger.warning("HyperDX source has no id to update", source=name)
            return None
        if not await client.update_source(source_id, spec):
            return None
        logger.info("HyperDX source updated", source=name, source_id=source_id)
        return source_id

    created = await client.create_source(spec)
    if not created:
        return None
    source_id = _document_id(created)
    if not source_id:
        logger.warning("HyperDX accepted the source but returned no id", source=name)
        return None
    logger.info("HyperDX source created", source=name, source_id=source_id)
    return source_id


async def remove_source(client: HyperDXClient, *, name: str) -> bool:
    """Delete the HyperDX source for a DFE source that is going away.

    Args:
        client: HyperDX client.
        name: DFE source name.

    Returns:
        True when the source is gone (deleted, or never there), False when
        HyperDX could not be read or refused the delete.
    """
    existing = await client.list_sources()
    if existing is None:
        return False

    current = next((s for s in existing if s.get("name") == name), None)
    if current is None:
        return True

    source_id = _document_id(current)
    if not source_id:
        logger.warning("HyperDX source has no id to delete", source=name)
        return False
    if not await client.delete_source(source_id):
        return False
    logger.info("HyperDX source removed", source=name, source_id=source_id)
    return True
