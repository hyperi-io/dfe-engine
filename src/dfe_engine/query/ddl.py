"""
DDLManager - Creates and maintains RBAC and parameterized views in ClickHouse.

Uses the admin connection to:
- Bootstrap restricted role, user, and settings profile
- Apply builtin parameterized views from .sql files
- Grant SELECT on views to the restricted role
- Diff live views against builtin definitions
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from scalo.logger import logger

from dfe_engine.query.catalog import VIEW_PREFIX

# Directory containing builtin .sql view definitions
BUILTIN_VIEWS_DIR = Path(__file__).parent / "builtin_views"

# The overall-ingest-throughput view. Engine-GENERATED (not a static .sql) because
# it must merge() over exactly the _timestamp_load-bearing data tables, whose set is
# dynamic - see DDLManager.apply_throughput_view.
_THROUGHPUT_VIEW = "dfe_v_overview_ingest_rows_bytes"


class DDLManager:
    """Manages ClickHouse parameterized-view DDL (apply / diff / drop).

    All operations use the admin connection. The restricted READER identity is
    no longer defined here - it is the ``query_reader`` service role reconciled by
    governance.ch.ChRbacReconciler (a single definition of the restricted reader).
    Views grant SELECT to that role (``dfe_query_reader_role``).

    Args:
        client: A clickhouse-connect client (admin connection)
        database: Target database for views
    """

    _READER_ROLE = "dfe_query_reader_role"

    def __init__(self, client: Any, database: str = "default"):
        self._client = client
        self._database = database

    def apply_view(self, name: str, sql: str) -> None:
        """Apply a single parameterized view and grant access.

        Args:
            name: View name (must start with dfe_v_ prefix)
            sql: CREATE OR REPLACE VIEW statement

        Raises:
            ValueError: If name doesn't match expected prefix
        """
        if not name.startswith(VIEW_PREFIX):
            raise ValueError(f"View name must start with '{VIEW_PREFIX}': {name}")

        try:
            self._client.command(self._prepare_sql(name, sql))
            self._grant_view(name)
            logger.info(f"Applied view: {self._database}.{name}")
        except Exception:
            logger.exception(f"Failed to apply view: {name}")
            raise

    def _prepare_sql(self, name: str, sql: str) -> str:
        """Render a builtin view's SQL against the target data database.

        The .sql files keep an UNQUALIFIED ``CREATE OR REPLACE VIEW dfe_v_...``
        head (the file is the SSoT / test contract) and use a ``{db}`` token for
        any table the hunt-runner materialises in the data database
        (hunt_schedule / hunt_state, whose db == effective_data_database, NOT the
        fixed schemas databases like dfe / dfe_audit). We qualify + substitute at
        APPLY time so:

        - P2.11: the view is created IN ``self._database`` (data db), where the
          catalog scans for it - an unqualified CREATE otherwise lands the view in
          the admin connection's session db and the catalog never discovers it.
        - P2.12: ``{db}.hunt_schedule`` resolves to the same db the runner writes,
          instead of a hardcoded ``dfe`` that UNKNOWN_DATABASE/TABLE-fails whenever
          effective_data_database differs.
        """
        rendered = sql.replace("{db}", self._database)
        # Qualify the CREATE head once (first VIEW <name> occurrence).
        return rendered.replace(f"VIEW {name}", f"VIEW {self._database}.{name}", 1)

    def _grant_view(self, name: str) -> None:
        """Grant SELECT on a view to the reconciled query_reader role."""
        self._client.command(f"GRANT SELECT ON {self._database}.{name} TO {self._READER_ROLE}")

    def _revoke_view(self, name: str) -> None:
        """Revoke SELECT on a view from the reconciled query_reader role."""
        try:
            self._client.command(
                f"REVOKE SELECT ON {self._database}.{name} FROM {self._READER_ROLE}"
            )
        except Exception:
            logger.warning(f"Failed to revoke view (may not exist): {name}")

    def drop_view(self, name: str) -> None:
        """Drop a view and revoke access.

        Args:
            name: View name to drop
        """
        self._revoke_view(name)
        try:
            self._client.command(f"DROP VIEW IF EXISTS {self._database}.{name}")
            logger.info(f"Dropped view: {self._database}.{name}")
        except Exception:
            logger.exception(f"Failed to drop view: {name}")
            raise

    def apply_all_builtin_views(self) -> list[str]:
        """Apply all builtin .sql view definitions.

        Reads all .sql files from the builtin_views/ directory,
        executes them, and grants access.

        Returns:
            List of view names that were applied
        """
        if not BUILTIN_VIEWS_DIR.is_dir():
            logger.warning(f"Builtin views directory not found: {BUILTIN_VIEWS_DIR}")
            return []

        applied: list[str] = []

        for sql_file in sorted(BUILTIN_VIEWS_DIR.glob("*.sql")):
            name = sql_file.stem  # e.g. dfe_v_system_health
            if name == _THROUGHPUT_VIEW:
                continue  # engine-generated below (the static single-table form is retired)
            sql = sql_file.read_text()

            try:
                self.apply_view(name, sql)
                applied.append(name)
            except Exception:
                logger.exception(f"Failed to apply builtin view: {sql_file.name}")

        # The overall-throughput view is ENGINE-GENERATED (see below), applied after
        # the static ones so it reflects the current source-table set. A live-sensing
        # failure here must not sink the static views.
        try:
            if self.apply_throughput_view():
                applied.append(_THROUGHPUT_VIEW)
        except Exception:
            logger.exception("Failed to apply generated throughput view")

        logger.info(f"Applied {len(applied)} builtin views")
        return applied

    def apply_throughput_view(self) -> bool:
        """Create the overall-ingest-throughput view (rows + approx bytes per time
        bucket) over EVERY ``_timestamp_load``-bearing table in the data database.

        Generated, NOT a static ``.sql``, on purpose: the data database holds the
        landing table + per-source data tables (all with ``_timestamp_load``) AND
        the hunt coordination tables (``hunt_lease``/``hunt_watermark``/
        ``hunt_state``/``hunt_schedule``, ``detection_checkpoint``) which do NOT.
        A ``merge(db, '.*')`` therefore ERRORS (a merged table lacks the column,
        code 10), and source-table names are arbitrary (= the source label), so no
        static regex catches exactly the data tables. So we SENSE the
        ``_timestamp_load`` tables from ``system.columns`` and ``merge()`` over an
        anchored alternation of exactly those - regenerated on each apply pass, so a
        newly promoted source appears the next time views are applied.

        Returns True when the view was created, False when there is no
        ``_timestamp_load`` table yet (nothing to aggregate - skip, don't error).
        """
        tables = self._timestamp_load_tables()
        if not tables:
            logger.info("Throughput view skipped: no _timestamp_load tables yet")
            return False
        self.apply_view(_THROUGHPUT_VIEW, self._render_throughput_sql(tables))
        return True

    def _timestamp_load_tables(self) -> list[str]:
        """Data-db table names that carry ``_timestamp_load`` (landing + sources)."""
        result = self._client.query(
            "SELECT table FROM system.columns "
            "WHERE database = {db:String} AND name = '_timestamp_load' ORDER BY table",
            parameters={"db": self._database},
        )
        return [row[0] for row in result.result_rows]

    @staticmethod
    def _render_throughput_sql(tables: list[str]) -> str:
        """The parameterised throughput view over ``merge()`` of exactly ``tables``.

        The ``{db}`` token + the CREATE head are qualified by ``apply_view``. Table
        names are regex-escaped and anchored (``^(...)$``) so only those exact
        tables merge - the coordination tables never match. ``re.escape`` neutralises
        regex metachars but NOT the SQL ``'`` that delimits the merge() pattern
        literal, and ``_timestamp_load_tables()`` reads EVERY table (not only
        validated source names), so a quote-bearing table name from out-of-band DDL
        could break out of the literal - double each single quote for the SQL string
        (belt-and-braces on top of the regex escape).
        """
        import re

        alternation = "|".join(re.escape(t).replace("'", "''") for t in tables)
        return (
            f"CREATE OR REPLACE VIEW {_THROUGHPUT_VIEW} AS\n"
            "SELECT\n"
            "    toStartOfInterval(_timestamp_load, INTERVAL {bucket_minutes:UInt32} MINUTE) AS bucket,\n"
            "    count() AS rows,\n"
            "    sum(length(coalesce(_raw, ''))) AS approx_bytes\n"
            f"FROM merge({{db}}, '^({alternation})$')\n"
            "WHERE _timestamp_load >= {time_from:DateTime64(3)}\n"
            "  AND _timestamp_load < {time_to:DateTime64(3)}\n"
            "GROUP BY bucket\n"
            "ORDER BY bucket"
        )

    def diff_views(self) -> dict[str, list[str]]:
        """Compare builtin .sql files against live views in ClickHouse.

        Returns:
            Dict with keys: "missing" (in files but not live),
            "extra" (live but not in files), "present" (in both)
        """
        # Get builtin view names from .sql files
        builtin_names: set[str] = set()
        if BUILTIN_VIEWS_DIR.is_dir():
            for sql_file in BUILTIN_VIEWS_DIR.glob("*.sql"):
                builtin_names.add(sql_file.stem)

        # Get live view names from ClickHouse
        like_pattern = f"{VIEW_PREFIX}%"
        try:
            result = self._client.query(
                "SELECT name FROM system.tables "
                "WHERE database = {db:String} "
                "AND name LIKE {prefix:String} "
                "AND engine = 'View'",
                parameters={"db": self._database, "prefix": like_pattern},
            )
            live_names = {row[0] for row in result.result_rows}
        except Exception:
            logger.exception("Failed to query live views for diff")
            live_names = set()

        return {
            "missing": sorted(builtin_names - live_names),
            "extra": sorted(live_names - builtin_names),
            "present": sorted(builtin_names & live_names),
        }

    def bootstrap(self) -> list[str]:
        """Apply all builtin views (the reader RBAC is the reconciler's job now).

        Returns:
            List of applied view names
        """
        return self.apply_all_builtin_views()
