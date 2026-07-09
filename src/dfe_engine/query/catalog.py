"""
ViewCatalog - Discovers parameterized views from ClickHouse system.tables.

Queries system.tables for views matching the dfe_v_ prefix, parses their
{param:Type} patterns, and builds a typed catalog for API consumers.
Results are cached with a configurable TTL.
"""

from __future__ import annotations

import re
import time
from typing import Any

from scalo.logger import logger

from dfe_engine.query.models import ViewDefinition, ViewParameter

# Regex to extract {param:Type} patterns from CREATE VIEW SQL
_PARAM_PATTERN = re.compile(r"\{(\w+):(\w+(?:\([^)]*\))?)\}")

# View name prefix
VIEW_PREFIX = "dfe_v_"

# ClickHouse type → (python_type, typescript_type, input_type)
_TYPE_MAP: dict[str, tuple[str, str, str]] = {
    "String": ("string", "string", "text"),
    "UUID": ("uuid", "string", "text"),
    "Bool": ("boolean", "boolean", "checkbox"),
    "Date": ("date", "string", "date"),
    "Date32": ("date", "string", "date"),
}

# Patterns matched by prefix
_TYPE_PREFIX_MAP: dict[str, tuple[str, str, str]] = {
    "UInt": ("integer", "number", "number"),
    "Int": ("integer", "number", "number"),
    "Float": ("float", "number", "number"),
    "Decimal": ("float", "number", "number"),
    "DateTime": ("datetime", "string", "datetime-local"),
    "Array": ("array", "string[]", "multiselect"),
}

# Reserved parameter names — injected server-side, hidden from UI
RESERVED_PARAMS = frozenset({"org_id"})


def map_clickhouse_type(ch_type: str) -> tuple[str, str, str]:
    """Map a ClickHouse type to (python_type, typescript_type, input_type).

    Args:
        ch_type: ClickHouse type string (e.g. "String", "UInt64", "Array(String)")

    Returns:
        Tuple of (python_type, typescript_type, input_type)
    """
    # Exact match first
    if ch_type in _TYPE_MAP:
        return _TYPE_MAP[ch_type]

    # Array type — refine typescript_type based on element type
    if ch_type.startswith("Array("):
        inner = ch_type[6:-1]  # Extract inner type
        inner_py, inner_ts, _ = map_clickhouse_type(inner)
        ts_type = f"{inner_ts}[]"
        return "array", ts_type, "multiselect"

    # Prefix match
    for prefix, type_tuple in _TYPE_PREFIX_MAP.items():
        if ch_type.startswith(prefix):
            return type_tuple

    # Fallback
    return "string", "string", "text"


def parse_view_parameters(create_sql: str) -> list[ViewParameter]:
    """Extract parameters from a CREATE VIEW statement.

    Parses {param:Type} patterns from the SQL and returns deduplicated
    ViewParameter instances with full type metadata.

    Args:
        create_sql: The CREATE VIEW SQL statement

    Returns:
        List of ViewParameter (deduplicated, preserving first occurrence order)
    """
    seen: set[str] = set()
    params: list[ViewParameter] = []

    for match in _PARAM_PATTERN.finditer(create_sql):
        name = match.group(1)
        ch_type = match.group(2)

        if name in seen:
            continue
        seen.add(name)

        python_type, typescript_type, input_type = map_clickhouse_type(ch_type)

        params.append(
            ViewParameter(
                name=name,
                clickhouse_type=ch_type,
                python_type=python_type,
                typescript_type=typescript_type,
                input_type=input_type,
                reserved=name in RESERVED_PARAMS,
            )
        )

    return params


def view_name_to_label(name: str) -> str:
    """Convert a view name to an API label.

    dfe_v_analytics_user_activity → analytics/user_activity

    The first segment after the prefix is the namespace, the rest is the name
    joined with underscores.

    Args:
        name: Full view name (e.g. dfe_v_analytics_user_activity)

    Returns:
        API label (e.g. analytics/user_activity)

    Raises:
        ValueError: If name doesn't match expected format
    """
    if not name.startswith(VIEW_PREFIX):
        raise ValueError(f"View name must start with '{VIEW_PREFIX}': {name}")

    suffix = name[len(VIEW_PREFIX) :]
    parts = suffix.split("_", 1)

    if len(parts) < 2:
        # Single segment — namespace is the name
        return parts[0]

    namespace, short_name = parts
    return f"{namespace}/{short_name}"


def label_to_view_name(label: str) -> str:
    """Convert an API label to a view name.

    analytics/user_activity → dfe_v_analytics_user_activity

    Args:
        label: API label (e.g. analytics/user_activity)

    Returns:
        Full view name (e.g. dfe_v_analytics_user_activity)
    """
    return VIEW_PREFIX + label.replace("/", "_")


class ViewCatalog:
    """Discovers and caches parameterized view definitions from ClickHouse.

    Queries system.tables for views matching the dfe_v_ prefix,
    parses parameters, and maintains a TTL-cached catalog.

    Args:
        client: A clickhouse-connect client (admin connection)
        database: ClickHouse database to scan
        cache_ttl: Cache time-to-live in seconds (default 60)
        view_prefix: View name prefix to filter (default "dfe_v_")
    """

    def __init__(
        self,
        client: Any,
        database: str = "default",
        cache_ttl: int = 60,
        view_prefix: str = VIEW_PREFIX,
    ):
        self._client = client
        self._database = database
        self._cache_ttl = cache_ttl
        self._view_prefix = view_prefix

        self._cache: dict[str, ViewDefinition] = {}
        self._cache_time: float = 0.0

    def _is_cache_valid(self) -> bool:
        """Check if the cached catalog is still within TTL."""
        return bool(self._cache) and (time.monotonic() - self._cache_time) < self._cache_ttl

    def refresh(self) -> None:
        """Force refresh of the view catalog from system.tables."""
        like_pattern = f"{self._view_prefix}%"

        try:
            result = self._client.query(
                "SELECT name, create_table_query, metadata_modification_time "
                "FROM system.tables "
                "WHERE database = {db:String} "
                "AND name LIKE {prefix:String} "
                "AND engine = 'View'",
                parameters={"db": self._database, "prefix": like_pattern},
            )
        except Exception:
            logger.exception("Failed to query system.tables for parameterized views")
            raise

        new_cache: dict[str, ViewDefinition] = {}

        for row in result.result_rows:
            name = row[0]
            create_sql = row[1]
            modified_at = str(row[2]) if row[2] else None

            try:
                label = view_name_to_label(name)
            except ValueError:
                logger.warning(f"Skipping view with unexpected name format: {name}")
                continue

            parts = label.split("/", 1)
            namespace = parts[0]
            short_name = parts[1] if len(parts) > 1 else parts[0]

            parameters = parse_view_parameters(create_sql)

            # Determine tenant isolation from parameters
            tenant_isolated = any(p.name == "org_id" for p in parameters)

            view_def = ViewDefinition(
                name=name,
                label=label,
                database=self._database,
                parameters=parameters,
                create_sql=create_sql,
                modified_at=modified_at,
                namespace=namespace,
                short_name=short_name,
                tenant_isolated=tenant_isolated,
            )

            new_cache[label] = view_def

        self._cache = new_cache
        self._cache_time = time.monotonic()
        logger.info(f"ViewCatalog refreshed: {len(new_cache)} views discovered")

    def _ensure_cache(self) -> None:
        """Refresh cache if expired."""
        if not self._is_cache_valid():
            self.refresh()

    def list_views(self, namespace: str | None = None) -> list[ViewDefinition]:
        """List all available parameterized views.

        Args:
            namespace: Optional namespace filter (e.g. "analytics")

        Returns:
            List of ViewDefinition
        """
        self._ensure_cache()

        views = list(self._cache.values())
        if namespace:
            views = [v for v in views if v.namespace == namespace]

        return sorted(views, key=lambda v: v.label)

    def get_view(self, label: str) -> ViewDefinition:
        """Get a specific view definition by label.

        Args:
            label: View label (e.g. "analytics/user_activity")

        Returns:
            ViewDefinition

        Raises:
            KeyError: If view not found
        """
        self._ensure_cache()

        if label not in self._cache:
            # Try a single refresh in case view was just created
            self.refresh()

        if label not in self._cache:
            available = sorted(self._cache.keys())
            raise KeyError(f"View '{label}' not found. Available views: {available}")

        return self._cache[label]

    def get_namespaces(self) -> list[str]:
        """Get list of unique namespaces.

        Returns:
            Sorted list of namespace strings
        """
        self._ensure_cache()
        return sorted({v.namespace for v in self._cache.values()})

    @property
    def database(self) -> str:
        """The database being scanned."""
        return self._database
