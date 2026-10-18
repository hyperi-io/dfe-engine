#  Project:      dfe-engine
#  File:         clickhouse/all_sources.py
#  Purpose:      One Merge table over the landing and per-source tables, rebuilt as the source set changes
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The ``all_sources`` Merge table, so one HyperDX source reads every source's rows.

A source with its own schema writes to its own table rather than the landing table, so a dashboard over the landing table alone never shows its events. ``all_sources`` is a ClickHouse Merge table over the landing table and every registry source's live table, rebuilt whenever that set changes. The HyperDX source over it is :mod:`dfe_engine.hyperdx.all_sources`.

The member list is explicit: the data database also holds the OTel tables, ``detection``, ``meta`` and per-source views, none of which belong in a source dashboard. A table without ``_org_id`` is left out too, since the tenant row policy on all_sources reads that column from every member. The landing table is the one the manifest names, and its live types give the shared header columns theirs.
"""

import re
import threading
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from scalo.logger import logger

from dfe_engine.clickhouse.quoting import quote_identifier, quote_literal
from dfe_engine.clickhouse.statements import ddl_settings
from dfe_engine.schema.applier import LiveTable, live_columns, live_tables, tables_with_column
from dfe_engine.schema.core_schema import landing_table_name
from dfe_engine.schema.engine_resolver import EngineResolver, parse_engine
from dfe_engine.schema.plan import data_table_names

if TYPE_CHECKING:
    from dfe_engine.settings import DFESettings
    from dfe_engine.source.models import Source

__all__ = [
    "ALL_SOURCES",
    "ALL_SOURCES_COLUMNS",
    "ALL_SOURCES_TIMESTAMP",
    "AllSourcesError",
    "Coalescer",
    "member_pattern",
    "member_tables",
    "merge_columns",
    "merge_table_ddl",
    "rebuild_all_sources",
    "try_rebuild_all_sources",
]

# The Merge table's name and its HyperDX source's name; a DFE source name cannot hold `_`, so no source can take it.
ALL_SOURCES = "all_sources"

# The header columns every source table shares, in the order the Merge table declares them.
ALL_SOURCES_COLUMNS = (
    "_timestamp_load",
    "_timestamp",
    "_timestamp_received",
    "_org_id",
    "_source",
    "_raw",
    "_json",
)

# A member without `_timestamp` reads it as 1970 through the Merge table, while every member has `_timestamp_load`.
ALL_SOURCES_TIMESTAMP = "_timestamp_load"

# The member list becomes a regex, so a name may only hold characters that match themselves there.
_PLAIN_NAME = re.compile(r"[A-Za-z0-9_-]+")

# A Merge table reads only real tables; views, dictionaries and other Merge tables are left out.
_MERGE_TREE_FAMILY = "MergeTree"

# The column the tenant row policy filters on, which every member must carry.
_TENANT_COLUMN = "_org_id"


class AllSourcesError(Exception):
    """ClickHouse does not hold what the all_sources Merge table needs."""


class Coalescer[T]:
    """Run one piece of work at a time, folding every piece handed in mid-run into one more run."""

    def __init__(self) -> None:
        """Start idle."""
        self._lock = threading.Lock()
        self._pending: Callable[[], T] | None = None
        self._running = False

    def run(self, *, work: Callable[[], T]) -> T | None:
        """Run *work* now, or hand it to the run in flight and return None at once.

        The caller already running runs the latest work handed to it once its own finishes, and returns or raises what that last run did.
        """
        with self._lock:
            if self._running:
                self._pending = work
                return None
            self._running = True
        failure = None
        outcome = None
        try:
            while True:
                try:
                    outcome, failure = work(), None
                except Exception as exc:  # work handed in meanwhile still runs; only the last run's failure is raised
                    outcome, failure = None, exc
                with self._lock:
                    if self._pending is None:
                        self._running = False
                        break
                    work, self._pending = self._pending, None
        except BaseException:
            with self._lock:
                self._pending = None
                self._running = False
            raise
        if failure is not None:
            raise failure
        return outcome


# Bulk deploys rebuild concurrently; the rebuild in flight takes over every request made meanwhile, so a burst costs one extra rebuild rather than one each.
_REBUILDS = Coalescer[list[str]]()


def _check_name(*, name: str) -> str:
    """Return *name* when it matches itself as a regex, else raise ValueError."""
    if not (_PLAIN_NAME.fullmatch(string=name)):
        raise ValueError(
            f"table name {name!r} must be one or more letters, digits, '_' or '-', so all_sources can list it by regex"
        )
    return name


def _fence(*, client: Any, database: str) -> None:
    """Apply the tenant row policy to all_sources, logging rather than raising when it cannot."""
    from dfe_engine.governance.ch.reconciler import fence_tables

    # The member tables' own policies still fence a read through the Merge table, so a missing one is not fatal.
    try:
        fence_tables(admin_client=client, database=database, tables=[ALL_SOURCES])
    except Exception as exc:
        logger.warning(
            "all_sources fence not applied", database=database, error=str(exc), table=ALL_SOURCES
        )


def _rebuild(
    *, client: Any, database: str, sources: Iterable[Source], topology: str | None
) -> list[str]:
    """Create or replace all_sources over the live member tables, fence it, and return the members."""
    landing_table = landing_table_name()
    members = member_tables(
        landing_table=landing_table,
        live=live_tables(client=client, database=database),
        reserved=data_table_names(data_database=database),
        sources=sources,
        with_org_id=tables_with_column(client=client, column=_TENANT_COLUMN, database=database),
    )
    columns = merge_columns(
        landing_columns=live_columns(client=client, database=database, table=landing_table),
        landing_table=landing_table,
    )
    resolver = EngineResolver(client=client, topology_setting=topology)
    on_cluster = resolver.resolve(
        database=database, spec=parse_engine(engine="MergeTree")
    ).on_cluster
    sql = merge_table_ddl(
        columns=columns, database=database, members=members, on_cluster=on_cluster
    )
    client.command(sql, settings=ddl_settings(statement=sql))
    # The tier grant is database-wide, so the table is readable the moment it exists and needs its row policy now.
    _fence(client=client, database=database)
    logger.info("all_sources rebuilt", database=database, members=members)
    return members


def member_pattern(*, members: Sequence[str]) -> str:
    """The table regex a Merge engine reads exactly *members* through, anchored at both ends."""
    if not (members):
        raise AllSourcesError("all_sources has no member table to read")
    names = [_check_name(name=name) for name in members]
    return "^(" + "|".join(names) + ")$"


def member_tables(
    *,
    landing_table: str,
    live: Mapping[str, LiveTable],
    reserved: Collection[str],
    sources: Iterable[Source],
    with_org_id: Collection[str],
) -> list[str]:
    """The landing table and each registry source's table that a live MergeTree-family table carrying ``_org_id`` backs, sorted.

    A source named after a table the manifest owns in the data database (*reserved*) does not pull that table in; the landing table is the one reserved member. A table missing from *with_org_id* is left out because the tenant row policy on all_sources would fail every tenant's read of it.
    """
    candidates = sorted({landing_table, *(source.table_name for source in sources)})
    for name in candidates:
        _check_name(name=name)
    return [
        name
        for name in candidates
        if name != ALL_SOURCES
        and ((name == landing_table) or (name not in reserved))
        and name in live
        and live[name].engine.endswith(_MERGE_TREE_FAMILY)
        and name in with_org_id
    ]


def merge_columns(
    *, landing_columns: Mapping[str, str], landing_table: str
) -> list[tuple[str, str]]:
    """The all_sources columns with the types the live landing table gives them."""
    missing = [name for name in ALL_SOURCES_COLUMNS if name not in landing_columns]
    if missing:
        raise AllSourcesError(
            f"landing table {landing_table!r} lacks the columns {missing!r}, which all_sources reads"
        )
    return [(name, landing_columns[name]) for name in ALL_SOURCES_COLUMNS]


def merge_table_ddl(
    *, columns: Sequence[tuple[str, str]], database: str, members: Sequence[str], on_cluster: str
) -> str:
    """The CREATE OR REPLACE statement for all_sources over *members*."""
    pattern = member_pattern(members=members)
    column_list = ", ".join(f"{quote_identifier(name=name)} {ch_type}" for name, ch_type in columns)
    db = quote_identifier(name=database)
    return (
        f"CREATE OR REPLACE TABLE {db}.{quote_identifier(name=ALL_SOURCES)}{on_cluster} "
        f"({column_list}) ENGINE = Merge({db}, {quote_literal(value=pattern)})"
    )


def rebuild_all_sources(
    *,
    client: Any,
    database: str,
    load_sources: Callable[[], Iterable[Source]],
    topology: str | None,
) -> list[str] | None:
    """Create or replace all_sources over the live member tables, fence it, and return the members.

    None means a rebuild already in flight in this process took this one over; it reads the sources again through *load_sources* once its own pass ends.
    """

    def rebuild() -> list[str]:
        """One pass over the sources as the registry holds them now."""
        return _rebuild(client=client, database=database, sources=load_sources(), topology=topology)

    return _REBUILDS.run(work=rebuild)


def try_rebuild_all_sources(
    *,
    changed: Sequence[str] = (),
    load_sources: Callable[[], Iterable[Source]],
    settings: DFESettings,
) -> list[str] | None:
    """Rebuild all_sources in this deployment's data database, logging a failure rather than raising it.

    *changed* names the sources whose write asked for the rebuild, for the log line a failure leaves.
    """
    from dfe_engine.governance.ch import ch_admin_client

    database = settings.clickhouse.effective_data_database
    # all_sources is a read view, so a stale one never fails a source write or the boot.
    try:
        return rebuild_all_sources(
            client=ch_admin_client(settings=settings),
            database=database,
            load_sources=load_sources,
            topology=settings.clickhouse.topology,
        )
    except Exception as exc:
        logger.warning(
            "all_sources not rebuilt", changed=list(changed), database=database, error=str(exc)
        )
        return None
