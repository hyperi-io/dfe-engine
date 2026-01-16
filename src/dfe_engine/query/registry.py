"""
Query Registry - Server-side query definition management.

Loads query definitions from PostgreSQL (primary) or YAML files (fallback).
Queries are Jinja2 templates with parameter validation schemas.
"""

from __future__ import annotations

import fnmatch
import hashlib
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from hs_pylib.logger import logger

from dfe_engine.query.models import (
    ParameterDefinition,
    QueryDefinition,
    QueryDefaults,
    QueryLimits,
)
from dfe_engine.yaml_utils import yaml_load

if TYPE_CHECKING:
    from jinja2 import Environment

# Protected stores that cannot be accessed even with store: "*"
PROTECTED_STORES: dict[str, set[str]] = {
    "clickhouse": {"system", "information_schema", "INFORMATION_SCHEMA"},
    "postgres": {"pg_catalog", "information_schema", "pg_toast"},
}


class QueryRegistryError(Exception):
    """Base exception for query registry errors."""

    pass


class QueryNotFoundError(QueryRegistryError):
    """Query label not found in registry."""

    pass


class QueryValidationError(QueryRegistryError):
    """Query definition failed validation."""

    pass


class QueryRegistry:
    """
    Registry of server-side query definitions.

    Supports two modes:
    1. PostgreSQL mode (primary): Loads from queries table, falls back to local files
    2. File mode: Loads only from YAML files

    Each query has:
    - A unique label (namespace/name format)
    - Jinja2 SQL template
    - Parameter schema for validation
    - Security constraints (roles, tenant isolation)
    - Execution limits

    Example YAML:
        queries:
          analytics/user_activity:
            datasource: clickhouse
            store: events
            sql: |
              SELECT user_id, count() as events
              FROM {{ store }}.events
              WHERE org_id = {{ _org_id }}
              LIMIT {{ limit }}
            parameters:
              event_type:
                type: string
                required: false
            tenant_isolated: true
            required_roles: [analyst]
    """

    _instance: QueryRegistry | None = None

    def __init__(
        self,
        postgres_dsn: str | None = None,
        fallback_directory: str | Path | None = None,
    ) -> None:
        """
        Initialize QueryRegistry.

        Args:
            postgres_dsn: PostgreSQL connection string for primary storage
            fallback_directory: Directory with YAML files for fallback/file-only mode
        """
        self._queries: dict[str, QueryDefinition] = {}
        self._jinja_env: Environment | None = None
        self._postgres_dsn = postgres_dsn
        self._fallback_directory = Path(fallback_directory) if fallback_directory else None
        self._pg_pool = None
        self._loaded_from_pg = False

    @classmethod
    def get_instance(
        cls,
        postgres_dsn: str | None = None,
        fallback_directory: str | Path | None = None,
    ) -> QueryRegistry:
        """
        Get singleton registry instance.

        Args:
            postgres_dsn: PostgreSQL DSN (only used on first call)
            fallback_directory: Fallback directory (only used on first call)
        """
        if cls._instance is None:
            cls._instance = QueryRegistry(
                postgres_dsn=postgres_dsn,
                fallback_directory=fallback_directory,
            )
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Reset singleton (for testing)."""
        if cls._instance and cls._instance._pg_pool:
            cls._instance._pg_pool.close()
        cls._instance = None

    @property
    def jinja_env(self) -> Environment:
        """Lazy-load Jinja2 environment with SQL-safe filters."""
        if self._jinja_env is None:
            from jinja2 import Environment, StrictUndefined

            self._jinja_env = Environment(
                undefined=StrictUndefined,
                autoescape=False,  # SQL, not HTML
            )
            # Register custom SQL filters
            self._jinja_env.filters["sql_string"] = self._filter_sql_string
            self._jinja_env.filters["sql_array"] = self._filter_sql_array
            self._jinja_env.filters["sql_identifier"] = self._filter_sql_identifier

        return self._jinja_env

    # -------------------------------------------------------------------------
    # PostgreSQL Backend
    # -------------------------------------------------------------------------

    def _get_pg_pool(self):
        """Lazy-load PostgreSQL connection pool."""
        if self._pg_pool is None and self._postgres_dsn:
            from psycopg_pool import ConnectionPool

            self._pg_pool = ConnectionPool(
                self._postgres_dsn,
                min_size=1,
                max_size=5,
                open=True,
            )
        return self._pg_pool

    def load(self, table_name: str = "query_definitions") -> int:
        """
        Load query definitions from best available source.

        Priority:
        1. PostgreSQL (if DSN configured and table exists)
        2. Fallback directory (YAML files)

        This is the recommended entry point for loading queries.
        Gracefully falls back to files if PostgreSQL is unavailable.

        Args:
            table_name: Name of the queries table in PostgreSQL

        Returns:
            Number of queries loaded

        Raises:
            QueryRegistryError: If no queries could be loaded from any source
        """
        # Try PostgreSQL first if configured
        if self._postgres_dsn:
            try:
                count = self._load_from_postgres_internal(table_name)
                if count > 0:
                    return count
                # PG succeeded but no queries - fall through to files
                logger.warning("PostgreSQL table exists but contains no enabled queries")
            except Exception as e:
                logger.warning(f"PostgreSQL unavailable, falling back to files: {e}")
        else:
            logger.debug("No PostgreSQL DSN configured, using file-based queries")

        # Fallback to local files
        if self._fallback_directory and self._fallback_directory.exists():
            count = self.load_from_directory(self._fallback_directory)
            if count > 0:
                return count
            logger.warning(f"No queries found in fallback directory: {self._fallback_directory}")
        elif self._fallback_directory:
            logger.warning(f"Fallback directory does not exist: {self._fallback_directory}")
        else:
            logger.warning("No fallback directory configured")

        if not self._queries:
            raise QueryRegistryError(
                "No queries loaded from any source. "
                "Configure PostgreSQL DSN or provide a fallback directory with query files."
            )

        return len(self._queries)

    def _load_from_postgres_internal(self, table_name: str) -> int:
        """
        Internal method to load from PostgreSQL.

        Args:
            table_name: Name of the queries table

        Returns:
            Number of queries loaded

        Raises:
            Exception: On any PostgreSQL error (connection, table missing, etc.)
        """
        pool = self._get_pg_pool()
        with pool.connection() as conn:
            with conn.cursor() as cur:
                # Check if table exists first
                cur.execute(
                    """
                    SELECT EXISTS (
                        SELECT FROM information_schema.tables
                        WHERE table_name = %s
                    )
                    """,
                    (table_name,),
                )
                table_exists = cur.fetchone()[0]

                if not table_exists:
                    raise QueryRegistryError(
                        f"Query definitions table '{table_name}' does not exist"
                    )

                cur.execute(
                    f"""
                    SELECT label, definition
                    FROM {table_name}
                    WHERE enabled = true
                    ORDER BY label
                    """
                )
                rows = cur.fetchall()

        count = 0
        for label, definition in rows:
            try:
                self._register_query(label, definition)
                count += 1
            except QueryValidationError as e:
                logger.warning(f"Skipping invalid query from PG: {e}")

        self._loaded_from_pg = True
        logger.info(f"Loaded {count} queries from PostgreSQL")
        return count

    def load_from_postgres(self, table_name: str = "query_definitions") -> int:
        """
        Load query definitions from PostgreSQL with graceful fallback.

        If PostgreSQL is not configured or unavailable, falls back to local files.

        Args:
            table_name: Name of the queries table

        Returns:
            Number of queries loaded

        Raises:
            QueryRegistryError: If both PG and fallback fail
        """
        if not self._postgres_dsn:
            logger.warning(
                "PostgreSQL DSN not configured for query registry, "
                "falling back to local files"
            )
            if self._fallback_directory and self._fallback_directory.exists():
                return self.load_from_directory(self._fallback_directory)
            raise QueryRegistryError(
                "PostgreSQL DSN not configured and no fallback directory available"
            )

        try:
            return self._load_from_postgres_internal(table_name)

        except Exception as e:
            logger.warning(f"Failed to load from PostgreSQL: {e}")

            # Fallback to local files
            if self._fallback_directory and self._fallback_directory.exists():
                logger.info("Falling back to local query files")
                return self.load_from_directory(self._fallback_directory)
            else:
                raise QueryRegistryError(
                    f"PostgreSQL unavailable and no fallback directory: {e}"
                ) from e

    def save_to_postgres(
        self,
        label: str,
        definition: dict[str, Any],
        table_name: str = "query_definitions",
    ) -> None:
        """
        Save a query definition to PostgreSQL.

        Args:
            label: Query label
            definition: Query definition dict
            table_name: Name of the queries table
        """
        if not self._postgres_dsn:
            raise QueryRegistryError("PostgreSQL DSN not configured")

        import json

        pool = self._get_pg_pool()
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    INSERT INTO {table_name} (label, definition, enabled, created_at, updated_at)
                    VALUES (%s, %s, true, NOW(), NOW())
                    ON CONFLICT (label) DO UPDATE SET
                        definition = EXCLUDED.definition,
                        updated_at = NOW()
                    """,
                    (label, json.dumps(definition)),
                )
            conn.commit()

        # Also register locally
        self._register_query(label, definition)

    def refresh_from_postgres(self, table_name: str = "query_definitions") -> int:
        """
        Refresh queries from PostgreSQL (hot reload).

        Returns:
            Number of queries loaded
        """
        self._queries.clear()
        return self.load_from_postgres(table_name)

    @staticmethod
    def create_postgres_table(dsn: str, table_name: str = "query_definitions") -> None:
        """
        Create the query definitions table in PostgreSQL.

        Args:
            dsn: PostgreSQL connection string
            table_name: Name for the table
        """
        import psycopg

        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {table_name} (
                        label VARCHAR(255) PRIMARY KEY,
                        definition JSONB NOT NULL,
                        enabled BOOLEAN DEFAULT true,
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                        updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                        created_by VARCHAR(255),
                        description TEXT
                    );

                    CREATE INDEX IF NOT EXISTS idx_{table_name}_enabled
                        ON {table_name} (enabled) WHERE enabled = true;

                    CREATE INDEX IF NOT EXISTS idx_{table_name}_updated
                        ON {table_name} (updated_at DESC);
                    """
                )
            conn.commit()

        logger.info(f"Created PostgreSQL table: {table_name}")

    # -------------------------------------------------------------------------
    # File Backend
    # -------------------------------------------------------------------------

    def load_from_file(self, path: str | Path) -> int:
        """
        Load query definitions from a YAML file.

        Args:
            path: Path to queries YAML file

        Returns:
            Number of queries loaded

        Raises:
            QueryValidationError: Invalid query definition
        """
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Query file not found: {path}")

        data = yaml_load(path)
        queries = data.get("queries", {})

        count = 0
        for label, definition in queries.items():
            self._register_query(label, definition)
            count += 1

        logger.info(f"Loaded {count} queries from {path}")
        return count

    def load_from_directory(self, directory: str | Path, pattern: str = "*.yaml") -> int:
        """
        Load query definitions from all YAML files in directory.

        Args:
            directory: Directory containing query YAML files
            pattern: Glob pattern for files to load

        Returns:
            Total number of queries loaded
        """
        directory = Path(directory)
        if not directory.is_dir():
            raise NotADirectoryError(f"Not a directory: {directory}")

        total = 0
        for path in sorted(directory.glob(pattern)):
            try:
                total += self.load_from_file(path)
            except Exception as e:
                logger.warning(f"Failed to load {path}: {e}")

        return total

    # -------------------------------------------------------------------------
    # Registration
    # -------------------------------------------------------------------------

    def _register_query(self, label: str, definition: dict[str, Any]) -> None:
        """Register a single query definition."""
        # Validate label format
        if not re.match(r"^[a-z][a-z0-9_]*(/[a-z][a-z0-9_]*)*$", label):
            raise QueryValidationError(
                f"Invalid query label '{label}': must be lowercase alphanumeric "
                "with underscores, optionally namespaced with '/'"
            )

        # Parse definition
        try:
            # Convert nested dicts to models
            if "parameters" in definition:
                definition["parameters"] = {
                    k: ParameterDefinition(**v) if isinstance(v, dict) else v
                    for k, v in definition["parameters"].items()
                }
            if "defaults" in definition and isinstance(definition["defaults"], dict):
                definition["defaults"] = QueryDefaults(**definition["defaults"])
            if "limits" in definition and isinstance(definition["limits"], dict):
                definition["limits"] = QueryLimits(**definition["limits"])

            query_def = QueryDefinition(**definition)
        except Exception as e:
            raise QueryValidationError(f"Invalid query definition '{label}': {e}") from e

        # Validate tenant isolation
        if query_def.tenant_isolated and "{{ _org_id }}" not in query_def.sql:
            raise QueryValidationError(
                f"Query '{label}' has tenant_isolated=true but SQL does not "
                "contain {{ _org_id }}. Either add org filter or set tenant_isolated=false."
            )

        # Validate Jinja2 template compiles
        try:
            self.jinja_env.from_string(query_def.sql)
        except Exception as e:
            raise QueryValidationError(
                f"Query '{label}' has invalid Jinja2 template: {e}"
            ) from e

        self._queries[label] = query_def
        logger.debug(f"Registered query: {label}")

    def register_query(self, label: str, definition: QueryDefinition) -> None:
        """Register a query definition programmatically."""
        self._queries[label] = definition

    # -------------------------------------------------------------------------
    # Lookup
    # -------------------------------------------------------------------------

    def get(self, label: str) -> QueryDefinition:
        """
        Get query definition by label.

        Args:
            label: Query label (e.g., 'analytics/user_activity')

        Returns:
            QueryDefinition

        Raises:
            QueryNotFoundError: Label not found
        """
        if label not in self._queries:
            raise QueryNotFoundError(f"Query not found: {label}")
        return self._queries[label]

    def exists(self, label: str) -> bool:
        """Check if query label exists."""
        return label in self._queries

    def list_queries(self, namespace: str | None = None) -> list[str]:
        """
        List all query labels, optionally filtered by namespace.

        Args:
            namespace: Optional namespace prefix (e.g., 'analytics')

        Returns:
            List of query labels
        """
        if namespace:
            prefix = f"{namespace}/"
            return [q for q in self._queries if q.startswith(prefix)]
        return list(self._queries.keys())

    def __len__(self) -> int:
        return len(self._queries)

    def __contains__(self, label: str) -> bool:
        return label in self._queries

    # -------------------------------------------------------------------------
    # Store Resolution
    # -------------------------------------------------------------------------

    def resolve_store(
        self,
        query_def: QueryDefinition,
        client_store: str | None,
        available_stores: list[str] | None = None,
    ) -> list[str]:
        """
        Resolve the target store(s) for a query.

        Args:
            query_def: Query definition
            client_store: Client-requested store (only if store='*')
            available_stores: List of available stores (for glob/regex matching)

        Returns:
            List of resolved store names

        Raises:
            QueryValidationError: Invalid store specification
        """
        store_spec = query_def.store

        # Client-specified store
        if store_spec == "*":
            if not client_store:
                raise QueryValidationError(
                    "Query requires client to specify store but none provided"
                )
            # Validate not protected - extract scheme from datasource (e.g., 'clickhouse:default' -> 'clickhouse')
            datasource_scheme = query_def.datasource.split(":")[0]
            protected = PROTECTED_STORES.get(datasource_scheme, set())
            if client_store.lower() in {s.lower() for s in protected}:
                raise QueryValidationError(
                    f"Access to store '{client_store}' is not permitted"
                )
            return [client_store]

        # Literal store name
        if not any(c in store_spec for c in "*?[/"):
            return [store_spec]

        # Regex pattern (starts with /)
        if store_spec.startswith("/") and store_spec.endswith("/"):
            pattern = re.compile(store_spec[1:-1])
            if not available_stores:
                raise QueryValidationError(
                    "Cannot resolve regex store pattern without available_stores list"
                )
            matches = [s for s in available_stores if pattern.match(s)]
            if not matches:
                raise QueryValidationError(
                    f"No stores match pattern: {store_spec}"
                )
            return matches

        # Glob pattern
        if not available_stores:
            raise QueryValidationError(
                "Cannot resolve glob store pattern without available_stores list"
            )
        matches = [s for s in available_stores if fnmatch.fnmatch(s, store_spec)]
        if not matches:
            raise QueryValidationError(f"No stores match pattern: {store_spec}")
        return matches

    # -------------------------------------------------------------------------
    # SQL Rendering
    # -------------------------------------------------------------------------

    def render_sql(
        self,
        query_def: QueryDefinition,
        params: dict[str, Any],
        store: str,
    ) -> str:
        """
        Render the Jinja2 SQL template with parameters.

        Args:
            query_def: Query definition
            params: Validated parameters (including _org_id, limit, etc.)
            store: Resolved store name

        Returns:
            Rendered SQL string
        """
        template = self.jinja_env.from_string(query_def.sql)

        # Add store to params for template access
        render_params = {**params, "store": store}

        return template.render(**render_params)

    def generate_cache_key(
        self,
        label: str,
        params: dict[str, Any],
        store: str,
    ) -> str:
        """
        Generate cache key for query + params.

        Args:
            label: Query label
            params: Parameters
            store: Store name

        Returns:
            Cache key string
        """
        import json

        # Sort params for deterministic key
        sorted_params = json.dumps(params, sort_keys=True, default=str)
        content = f"{label}:{store}:{sorted_params}"
        return hashlib.sha256(content.encode()).hexdigest()[:32]

    # -------------------------------------------------------------------------
    # Jinja2 Filters
    # -------------------------------------------------------------------------

    @staticmethod
    def _filter_sql_string(value: Any) -> str:
        """Escape value as SQL string literal."""
        if value is None:
            return "NULL"
        escaped = str(value).replace("'", "''")
        return f"'{escaped}'"

    @staticmethod
    def _filter_sql_array(values: list[Any]) -> str:
        """Format list as SQL array/tuple for IN clauses."""
        if not values:
            return "(NULL)"  # Empty IN clause
        escaped = []
        for v in values:
            if isinstance(v, str):
                escaped.append(f"'{v.replace(chr(39), chr(39)+chr(39))}'")
            elif v is None:
                escaped.append("NULL")
            else:
                escaped.append(str(v))
        return f"({', '.join(escaped)})"

    @staticmethod
    def _filter_sql_identifier(value: str) -> str:
        """Quote SQL identifier (table/column name)."""
        # Remove any existing quotes and dangerous chars
        clean = re.sub(r"[^a-zA-Z0-9_]", "", value)
        return f'"{clean}"'

    def close(self) -> None:
        """Close PostgreSQL connection pool."""
        if self._pg_pool:
            self._pg_pool.close()
            self._pg_pool = None


# Module-level convenience functions
def get_registry() -> QueryRegistry:
    """Get the singleton query registry."""
    return QueryRegistry.get_instance()


def get_query(label: str) -> QueryDefinition:
    """Get query definition by label."""
    return get_registry().get(label)
