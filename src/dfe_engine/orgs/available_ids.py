#  Project:      dfe-engine
#  File:         orgs/available_ids.py
#  Purpose:      The tenant ids the deployment's data has actually carried
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Distinct ``_org_id`` values across the deployment's data tables.

Backs the org form's Organisation IDs suggestions, so an operator picks a tenant
id the data has seen rather than typing one that matches nothing. The tables are
discovered from ``system.columns`` -- every DFE table carrying the common header
has ``_org_id``, and a table without it holds no tenant rows to suggest from.
"""

from __future__ import annotations

import re
from typing import Any

from scalo.logger import logger

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

ORG_ID_COLUMN = "_org_id"

DEFAULT_LIMIT = 200
"""Distinct ids returned at most -- a suggestion list, not an export."""

DEFAULT_TIMEOUT_SECONDS = 10


class OrgIdDiscoveryError(Exception):
    """Raised when the tenant ids cannot be read from ClickHouse."""


def _safe_identifier(value: str) -> str:
    """A database or table name that can be quoted into SQL.

    The names come from ``system.columns``, so they are whatever a ClickHouse
    client created; anything outside the identifier shape is skipped rather than
    quoted, because a quoted surprise is still a surprise.
    """
    if not _IDENT.fullmatch(value):
        raise ValueError(f"unsafe identifier: {value!r}")
    return value


def _org_id_tables(client: Any, database: str) -> list[str]:
    """Tables in *database* carrying an ``_org_id`` column."""
    rows = client.execute(
        "SELECT table FROM system.columns "
        "WHERE database = {db:String} AND name = {col:String} "
        "GROUP BY table ORDER BY table",
        parameters={"db": database, "col": ORG_ID_COLUMN},
    )
    tables = []
    for row in rows or []:
        try:
            tables.append(_safe_identifier(str(row[0])))
        except ValueError:
            logger.warning(f"skipping table with an unquotable name in {database}")
    return tables


def available_org_ids(
    client: Any,
    *,
    database: str,
    limit: int = DEFAULT_LIMIT,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> list[str]:
    """Every tenant id present in *database*, sorted, empty values dropped.

    Returns an empty list when no table in the database carries ``_org_id``,
    which is what a deployment with no ingested data looks like.

    Raises:
        OrgIdDiscoveryError: when ClickHouse refuses either query.
    """
    db = _safe_identifier(database)
    try:
        tables = _org_id_tables(client, db)
    except Exception as exc:
        raise OrgIdDiscoveryError(f"cannot list the tenant tables in {db}: {exc}") from exc

    if not tables:
        return []

    # One DISTINCT per table, unioned: _org_id is a LowCardinality key column, so
    # each leg reads one column rather than the rows behind it.
    legs = " UNION DISTINCT ".join(
        f"SELECT DISTINCT `{ORG_ID_COLUMN}` AS org_id FROM `{db}`.`{table}`" for table in tables
    )
    sql = f"SELECT org_id FROM ({legs}) WHERE org_id != '' ORDER BY org_id LIMIT {int(limit)}"

    try:
        rows = client.execute(sql, settings={"max_execution_time": timeout_seconds})
    except Exception as exc:
        raise OrgIdDiscoveryError(f"cannot read the tenant ids in {db}: {exc}") from exc

    return [str(row[0]) for row in rows or []]
