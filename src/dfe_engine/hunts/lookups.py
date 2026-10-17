#  Project:      dfe-engine
#  File:         hunts/lookups.py
#  Purpose:      Prove the databases a rule may look values up in hold no org's rows
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The lookup databases a rule may read, checked against what ClickHouse holds in them.

``hunts.lookup_databases`` lets a rule's condition read another table, because a
listed database is shared reference data such as threat-intel indicators. The hunt
runner's ClickHouse user reads every org, so a listed database holding a table with
an ``_org_id`` column, or a table on any engine but a plain store of its own rows (a
view, a ``Merge``, a ``Distributed``, a ``Set``), could answer a rule from every org.

The hunt runner refuses to start while one does, and stops at the reload that first
finds one. The engine refuses to start when ClickHouse answers at its start, and
starts with the check unrun, logging a warning, when it does not. The check sees only
the tables the connecting user can see, which are the ones it could read.
"""

from collections.abc import Callable, Collection
from typing import Any

from scalo.logger import logger

from dfe_engine.http_errors import describe_failure
from dfe_engine.orgs.available_ids import ORG_ID_COLUMN

# Plain stores of their own rows beside the MergeTree family, which every *MergeTree name is.
_ROW_HOLDING_ENGINES = frozenset({"EmbeddedRocksDB", "Log", "Memory", "StripeLog", "TinyLog"})


class LookupDatabaseError(RuntimeError):
    """A listed lookup database holds a table that can carry an org's rows."""


def _holds_its_own_rows(engine: str) -> bool:
    """Whether ``engine`` is a plain store a rule may read, answering from rows written into it."""
    return engine.endswith("MergeTree") or engine in _ROW_HOLDING_ENGINES


def tenant_reachable_tables(client: Any, databases: Collection[str]) -> list[str]:
    """Each table of ``databases`` a rule may not read, with why.

    Args:
        client: A clickhouse-connect client, as the engine or the hunt runner connects.
        databases: ``hunts.lookup_databases``.

    Returns:
        One ``<db>.<table> ...`` line per table carrying an ``_org_id`` column or on an
        engine that is not a plain store of its own rows, in database and table order.
        Empty when there is nothing listed or nothing to refuse.
    """
    listed = list(databases)
    if not listed:
        return []
    with_org = client.query(
        "SELECT database, table FROM system.columns "
        "WHERE database IN {databases:Array(String)} AND name = {column:String} "
        "ORDER BY database, table",
        parameters={"databases": listed, "column": ORG_ID_COLUMN},
    ).result_rows
    tables = client.query(
        "SELECT database, name, engine FROM system.tables "
        "WHERE database IN {databases:Array(String)} ORDER BY database, name",
        parameters={"databases": listed},
    ).result_rows
    found = [f"{db}.{table} has an {ORG_ID_COLUMN} column" for db, table in with_org]
    found += [
        f"{db}.{name} is a {engine} table, not a plain store of its own rows"
        for db, name, engine in tables
        if not _holds_its_own_rows(engine)
    ]
    return found


def refuse_tenant_lookups(client: Any, databases: Collection[str]) -> None:
    """Raise when a listed lookup database holds a table a rule may not read.

    Args:
        client: A clickhouse-connect client.
        databases: ``hunts.lookup_databases``.

    Raises:
        LookupDatabaseError: Naming each such table.
    """
    found = tenant_reachable_tables(client, databases)
    if found:
        raise LookupDatabaseError(_refusal(found))


def _refusal(found: list[str]) -> str:
    """The refusal naming each table :func:`tenant_reachable_tables` found."""
    return (
        "hunts.lookup_databases lists a database holding a table a rule may not read, "
        f"since the hunt runner reads every org: {', '.join(found)}. Move the table out, "
        "or take its database off the list"
    )


def refuse_tenant_lookups_if_reachable(
    connect: Callable[[], Any], databases: Collection[str]
) -> None:
    """:func:`refuse_tenant_lookups` at the engine's start, or a log line if ClickHouse is down.

    The hunt runner waits for ClickHouse and checks before its first fire, so an engine
    that starts first leaves the check to it.

    Args:
        connect: Builds the engine's ClickHouse client.
        databases: ``hunts.lookup_databases``.

    Raises:
        LookupDatabaseError: Naming each table a rule may not read.
    """
    if not databases:
        return
    try:
        found = tenant_reachable_tables(connect(), databases)
    except Exception as exc:  # an unreachable ClickHouse is checked again by the hunt runner
        logger.warning(
            "lookup databases not checked: ClickHouse did not answer, the hunt runner "
            "checks them before it runs",
            databases=list(databases),
            **describe_failure(exc),
        )
        return
    if found:
        raise LookupDatabaseError(_refusal(found))
