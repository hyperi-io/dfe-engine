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

from hyperi_pylib.logger import logger

from dfe_engine.query.catalog import VIEW_PREFIX

# Directory containing builtin .sql view definitions
BUILTIN_VIEWS_DIR = Path(__file__).parent / "builtin_views"


class DDLManager:
    """Manages ClickHouse RBAC and parameterized view DDL.

    All operations use the admin connection. The restricted user/role
    created here is used by ViewExecutor for query execution.

    Args:
        client: A clickhouse-connect client (admin connection)
        database: Target database for views
        restricted_user: Username for the restricted query user
        restricted_password: Password for the restricted query user
        max_execution_time: Max query execution time in seconds
        max_rows_to_read: Max rows a query can scan
        max_memory_usage: Max memory per query (e.g. "2G")
    """

    def __init__(
        self,
        client: Any,
        database: str = "default",
        restricted_user: str = "dfe_query_user",
        restricted_password: str = "",
        max_execution_time: int = 30,
        max_rows_to_read: int = 10_000_000,
        max_memory_usage: str = "2G",
    ):
        self._client = client
        self._database = database
        self._restricted_user = restricted_user
        self._restricted_password = restricted_password
        self._max_execution_time = max_execution_time
        self._max_rows_to_read = max_rows_to_read
        self._max_memory_usage = max_memory_usage

    def ensure_rbac(self) -> None:
        """Bootstrap the restricted role, settings profile, and user.

        All statements are idempotent (IF NOT EXISTS).
        """
        statements = self._build_rbac_statements()

        for stmt in statements:
            try:
                self._client.command(stmt)
            except Exception:
                logger.exception(f"Failed to execute RBAC DDL: {stmt[:100]}...")
                raise

        logger.info(
            f"RBAC bootstrapped: user={self._restricted_user}, "
            f"max_execution_time={self._max_execution_time}, "
            f"max_rows_to_read={self._max_rows_to_read}"
        )

    def _build_rbac_statements(self) -> list[str]:
        """Build the RBAC bootstrap SQL statements.

        Returns:
            List of SQL statements to execute
        """
        return [
            (
                f"CREATE SETTINGS PROFILE IF NOT EXISTS dfe_query_profile "
                f"SETTINGS "
                f"readonly = 1 CONST, "
                f"max_execution_time = {self._max_execution_time} CONST, "
                f"max_rows_to_read = {self._max_rows_to_read} CONST, "
                f"max_memory_usage = '{self._max_memory_usage}' CONST, "
                f"allow_ddl = 0 CONST"
            ),
            "CREATE ROLE IF NOT EXISTS dfe_query_reader",
            (
                f"CREATE USER IF NOT EXISTS {self._restricted_user} "
                f"IDENTIFIED WITH sha256_password BY '{self._restricted_password}' "
                f"DEFAULT ROLE dfe_query_reader "
                f"SETTINGS PROFILE dfe_query_profile"
            ),
        ]

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
            self._client.command(sql)
            self._grant_view(name)
            logger.info(f"Applied view: {self._database}.{name}")
        except Exception:
            logger.exception(f"Failed to apply view: {name}")
            raise

    def _grant_view(self, name: str) -> None:
        """Grant SELECT on a view to the restricted role."""
        self._client.command(f"GRANT SELECT ON {self._database}.{name} TO dfe_query_reader")

    def _revoke_view(self, name: str) -> None:
        """Revoke SELECT on a view from the restricted role."""
        try:
            self._client.command(f"REVOKE SELECT ON {self._database}.{name} FROM dfe_query_reader")
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
            sql = sql_file.read_text()

            try:
                self.apply_view(name, sql)
                applied.append(name)
            except Exception:
                logger.exception(f"Failed to apply builtin view: {sql_file.name}")

        logger.info(f"Applied {len(applied)} builtin views")
        return applied

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
        """Full bootstrap: RBAC + all builtin views.

        Convenience method that runs ensure_rbac() followed by
        apply_all_builtin_views().

        Returns:
            List of applied view names
        """
        self.ensure_rbac()
        return self.apply_all_builtin_views()
