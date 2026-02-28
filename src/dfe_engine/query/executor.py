"""
ViewExecutor - Executes parameterized views via the restricted ClickHouse connection.

Security enforcement:
- org_id is ALWAYS injected from AuthContext (clients cannot set it)
- limit is capped at max_limit
- Role checks against view.required_roles
- All parameters are sent via clickhouse-connect server-side binding
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from dfe_engine.query.catalog import RESERVED_PARAMS, ViewCatalog
from dfe_engine.query.models import (
    AuthContext,
    AuthorizationError,
    QueryMetadata,
    QueryOptions,
    ViewDefinition,
)
from dfe_engine.query.result import QueryResult


class ViewExecutionError(Exception):
    """View execution failed."""

    pass


class ViewExecutor:
    """Executes parameterized views via a restricted ClickHouse connection.

    The restricted connection can ONLY SELECT from dfe_v_* views,
    enforced by ClickHouse RBAC. This class adds Python-side security:

    - org_id injection from JWT (always, cannot be overridden)
    - limit enforcement (capped at max_limit)
    - Role-based access control
    - Pagination wrapping (offset-based or keyset-based)

    Args:
        restricted_client: clickhouse-connect client authenticated as dfe_query_user
        catalog: ViewCatalog for view discovery
        database: ClickHouse database containing the views
        default_limit: Default row limit if not specified
        max_limit: Maximum allowed row limit
        default_timeout: Default query timeout in seconds
        max_timeout: Maximum allowed timeout in seconds
    """

    def __init__(
        self,
        restricted_client: Any,
        catalog: ViewCatalog,
        database: str = "default",
        default_limit: int = 1000,
        max_limit: int = 100_000,
        default_timeout: int = 30,
        max_timeout: int = 300,
    ):
        self._client = restricted_client
        self._catalog = catalog
        self._database = database
        self._default_limit = default_limit
        self._max_limit = max_limit
        self._default_timeout = default_timeout
        self._max_timeout = max_timeout

    def execute(
        self,
        label: str,
        params: dict[str, Any] | None,
        auth: AuthContext,
        options: QueryOptions | None = None,
    ) -> QueryResult:
        """Execute a parameterized view and return results.

        Args:
            label: View label (e.g. "analytics/user_activity")
            params: Client-provided parameters
            auth: Authentication context from JWT
            options: Query options (limit, offset, timeout, etc.)

        Returns:
            QueryResult with Arrow table and metadata

        Raises:
            KeyError: View not found
            AuthorizationError: User not authorized
            ViewExecutionError: Query execution failed
        """
        view_def = self._catalog.get_view(label)
        options = options or QueryOptions()

        # Authorization check
        self._check_authorization(view_def, auth)

        # Build final parameters
        final_params = self._build_params(view_def, params or {}, auth, options)

        # Resolve limit and offset
        limit = self._resolve_limit(options)
        offset = options.offset or 0

        # Build SQL
        sql = self._build_sql(view_def, final_params, limit, offset, options)

        # Resolve timeout
        timeout = self._resolve_timeout(options)

        # Execute
        start = time.perf_counter()
        try:
            settings = {"max_execution_time": timeout}
            table = self._client.query_arrow(
                sql,
                parameters=final_params,
                settings=settings,
            )
        except Exception as e:
            raise ViewExecutionError(
                f"Failed to execute view '{label}': {e}"
            ) from e

        duration_ms = int((time.perf_counter() - start) * 1000)

        # Build metadata
        has_more = table.num_rows >= limit
        metadata = QueryMetadata(
            row_count=table.num_rows,
            query_duration_ms=duration_ms,
            query_label=label,
            datasource="clickhouse",
            store=self._database,
            request_id=auth.request_id or str(uuid.uuid4()),
            has_more=has_more,
            next_offset=offset + table.num_rows if has_more else None,
        )

        return QueryResult(table=table, metadata=metadata)

    def list_views(self, namespace: str | None = None) -> list[ViewDefinition]:
        """List available views, optionally filtered by namespace.

        Args:
            namespace: Optional namespace filter

        Returns:
            List of ViewDefinition
        """
        return self._catalog.list_views(namespace=namespace)

    def get_view(self, label: str) -> ViewDefinition:
        """Get a specific view definition.

        Args:
            label: View label

        Returns:
            ViewDefinition
        """
        return self._catalog.get_view(label)

    def _check_authorization(
        self, view_def: ViewDefinition, auth: AuthContext
    ) -> None:
        """Check if user is authorized to execute this view."""
        if view_def.required_roles:
            if not any(role in auth.roles for role in view_def.required_roles):
                raise AuthorizationError(
                    f"View '{view_def.label}' requires one of roles: "
                    f"{view_def.required_roles}"
                )

        # Non-tenant-isolated views require admin role
        if not view_def.tenant_isolated and "admin" not in auth.roles:
            raise AuthorizationError(
                f"View '{view_def.label}' is not tenant-isolated and requires admin role"
            )

    def _build_params(
        self,
        view_def: ViewDefinition,
        client_params: dict[str, Any],
        auth: AuthContext,
        options: QueryOptions,
    ) -> dict[str, Any]:
        """Build the final parameter dict for query execution.

        Reserved parameters (org_id) are injected from auth context
        and cannot be overridden by the client.

        Args:
            view_def: View definition
            client_params: Client-supplied parameters
            auth: Auth context from JWT
            options: Query options

        Returns:
            Merged parameter dict
        """
        final: dict[str, Any] = {}

        # Inject reserved parameters — ALWAYS from auth, never from client
        final["org_id"] = auth.org_id

        # Add client parameters (excluding reserved ones)
        for param_def in view_def.parameters:
            name = param_def.name
            if name in RESERVED_PARAMS:
                continue
            if name in client_params:
                final[name] = client_params[name]

        # Inject standard options as parameters if view expects them
        param_names = {p.name for p in view_def.parameters}
        if "limit" in param_names:
            final["limit"] = self._resolve_limit(options)
        if "time_from" in param_names and options.time_from:
            final["time_from"] = options.time_from
        if "time_to" in param_names and options.time_to:
            final["time_to"] = options.time_to

        return final

    def _build_sql(
        self,
        view_def: ViewDefinition,
        params: dict[str, Any],
        limit: int,
        offset: int,
        options: QueryOptions,
    ) -> str:
        """Build the SQL statement for view execution.

        Generates: SELECT * FROM db.view_name(param1={param1:Type}, ...)
        with optional pagination wrapping.

        Args:
            view_def: View definition
            params: Final parameter dict
            limit: Resolved limit
            offset: Resolved offset
            options: Query options

        Returns:
            SQL string with {param:Type} placeholders for server-side binding
        """
        # Build parameter call syntax: view_name(param1={param1:Type}, ...)
        param_parts = []
        for p in view_def.parameters:
            param_parts.append(f"{p.name}={{{p.name}:{p.clickhouse_type}}}")

        param_str = ", ".join(param_parts)
        view_call = f"{self._database}.{view_def.name}({param_str})"

        # Check if view already has limit parameter — if so, just SELECT from it
        view_has_limit = any(p.name == "limit" for p in view_def.parameters)

        if view_has_limit and offset == 0 and not options.after_key:
            # View handles its own limit, no wrapping needed
            return f"SELECT * FROM {view_call}"

        # Wrap with pagination
        if options.after_key is not None and options.order_by:
            # Keyset pagination
            order_dir = options.order_dir or "asc"
            op = ">" if order_dir == "asc" else "<"
            return (
                f"SELECT * FROM {view_call} "
                f"WHERE {options.order_by} {op} {{_after_key}} "
                f"ORDER BY {options.order_by} {order_dir} "
                f"LIMIT {limit}"
            )

        if offset > 0:
            # Offset pagination
            return (
                f"SELECT * FROM {view_call} "
                f"LIMIT {limit} OFFSET {offset}"
            )

        # No pagination wrapping, but add outer limit if view doesn't have one
        return f"SELECT * FROM {view_call} LIMIT {limit}"

    def _resolve_limit(self, options: QueryOptions) -> int:
        """Resolve the effective limit, capped at max_limit."""
        if options.limit is not None:
            return min(options.limit, self._max_limit)
        return self._default_limit

    def _resolve_timeout(self, options: QueryOptions) -> int:
        """Resolve the effective timeout, capped at max_timeout."""
        if options.timeout_seconds is not None:
            return min(options.timeout_seconds, self._max_timeout)
        return self._default_timeout
