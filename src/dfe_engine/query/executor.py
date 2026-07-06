#  Project:      dfe-engine
#  File:         src/dfe_engine/query/executor.py
#  Purpose:      Secure parameterized view executor with RBAC
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""ViewExecutor — executes parameterized views via restricted ClickHouse connection.

Security enforcement:
- org_id is ALWAYS injected from AuthContext (clients cannot set it)
- limit is capped at max_limit
- Role checks against view.required_roles
- All parameters are sent via clickhouse-connect server-side binding
"""

from __future__ import annotations

import re
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

# A keyset ORDER BY / WHERE column is interpolated into the outer wrapper query -
# ClickHouse binds VALUES, not identifiers, so a column name cannot be a bound
# parameter and MUST instead be allowlisted. A free-string order_by is otherwise a
# SQL-injection sink on this path (F-QUERY-ORDERBY): the wrapper runs as the
# restricted reader which holds NO org role and is targeted by NO row policy, so an
# injected order_by reads every org's rows. Accept only a single bare column
# identifier; reject anything with whitespace, quotes, parens, commas, operators or
# SQL comments before it reaches the SQL string.
_SAFE_ORDER_BY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _after_key_ch_type(value: Any) -> str:
    """ClickHouse bind type for a keyset ``after_key`` cursor value.

    ``after_key`` compares against the order column, so its literal must bind with a
    type ClickHouse will compare: a bare String bind would make ``col > 'literal'``
    a type error on a numeric column. Infer from the Python value and fall back to
    String (which also covers ISO date/uuid/text order keys). bool is checked
    before int because ``bool`` is a subclass of ``int``.
    """
    if isinstance(value, bool):
        return "Bool"
    if isinstance(value, int):
        return "Int64"
    if isinstance(value, float):
        return "Float64"
    return "String"


class ViewExecutionError(Exception):
    """View execution failed."""


class ViewExecutor:
    """Executes parameterized views via a restricted ClickHouse connection.

    Args:
        restricted_client: clickhouse-connect client authenticated as dfe_query_reader
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
        """Execute a parameterized view and return results."""
        view_def = self._catalog.get_view(label)
        options = options or QueryOptions()

        self._check_authorization(view_def, auth)

        final_params = self._build_params(view_def, params or {}, auth, options)
        limit = self._resolve_limit(options)
        offset = options.offset or 0
        sql = self._build_sql(view_def, final_params, limit, offset, options)
        timeout = self._resolve_timeout(options)

        start = time.perf_counter()
        try:
            settings = {"max_execution_time": timeout}
            result = self._client.query(
                sql,
                parameters=final_params,
                settings=settings,
            )
        except Exception as e:
            raise ViewExecutionError(f"Failed to execute view '{label}': {e}") from e

        duration_ms = int((time.perf_counter() - start) * 1000)

        columns = result.column_names
        rows = [dict(zip(columns, row, strict=True)) for row in result.result_rows]

        has_more = len(rows) >= limit
        metadata = QueryMetadata(
            row_count=len(rows),
            query_duration_ms=duration_ms,
            query_label=label,
            datasource="clickhouse",
            store=self._database,
            request_id=auth.request_id or str(uuid.uuid4()),
            has_more=has_more,
            next_offset=offset + len(rows) if has_more else None,
        )

        return QueryResult(rows=rows, columns=columns, metadata=metadata)

    def list_views(self, namespace: str | None = None) -> list[ViewDefinition]:
        """List available views, optionally filtered by namespace."""
        return self._catalog.list_views(namespace=namespace)

    def get_view(self, label: str) -> ViewDefinition:
        """Get a specific view definition."""
        return self._catalog.get_view(label)

    def get_namespaces(self) -> list[str]:
        """List available view namespaces (public accessor over the catalog)."""
        return self._catalog.get_namespaces()

    def _check_authorization(self, view_def: ViewDefinition, auth: AuthContext) -> None:
        from dfe_engine.auth import Scope, authorize
        from dfe_engine.settings import get_settings

        settings = get_settings()
        # query:execute is a tenant action: allow if the caller holds it at system
        # OR any org they belong to, so an org-scoped viewer reaches their own org's
        # views instead of this system-only re-check denying them after the endpoint
        # already authorized. (Sharing this with deps._authorize_resolved via one
        # auth-layer resolver is a noted DRY follow-on.)
        candidate_scopes: list[Scope | None] = [None]
        candidate_scopes += [
            Scope(type="org", id=org)
            for org in (auth.org_ids or ([auth.org_id] if auth.org_id else []))
        ]
        allowed = False
        reason = "no query:execute grant at the caller's scope"
        for cand in candidate_scopes:
            r = authorize(
                auth, "query:execute", view_def.label, scope=cand, enabled=settings.auth.enabled
            )
            if r.allowed:
                allowed = True
                break
            reason = r.reason
        if not allowed:
            raise AuthorizationError(f"View '{view_def.label}': access denied ({reason})")

        if view_def.required_roles:
            if not any(role in auth.roles for role in view_def.required_roles):
                raise AuthorizationError(
                    f"View '{view_def.label}' requires one of roles: {view_def.required_roles}"
                )

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
        final: dict[str, Any] = {}
        final["org_id"] = auth.org_id

        for param_def in view_def.parameters:
            name = param_def.name
            if name in RESERVED_PARAMS:
                continue
            if name in client_params:
                final[name] = client_params[name]

        param_names = {p.name for p in view_def.parameters}
        if "limit" in param_names:
            page_size = self._resolve_limit(options)
            offset = options.offset or 0
            if offset > 0 and options.after_key is None:
                # Offset pagination: the inner view limit must cover the whole
                # requested window (offset + page size), or the outer
                # LIMIT/OFFSET in _build_sql slices an already-truncated inner
                # result and page 2+ comes back silently empty. Capped at
                # max_limit so a huge offset cannot bypass the documented cap and
                # force the inner view to scan unboundedly (P3.13).
                final["limit"] = min(offset + page_size, self._max_limit)
            else:
                final["limit"] = page_size
        if "time_from" in param_names and options.time_from:
            final["time_from"] = options.time_from
        if "time_to" in param_names and options.time_to:
            final["time_to"] = options.time_to

        # Keyset pagination binds the cursor value server-side (F-QUERY-ORDERBY):
        # the outer wrapper's WHERE {col} > {_after_key:T} is a bound parameter,
        # never an interpolated literal. Add it only when the keyset SQL branch
        # will actually fire (both after_key and order_by present), matching
        # _build_sql's condition exactly so clickhouse-connect is never handed an
        # unreferenced parameter.
        if options.after_key is not None and options.order_by:
            final["_after_key"] = options.after_key

        return final

    def _build_sql(
        self,
        view_def: ViewDefinition,
        params: dict[str, Any],
        limit: int,
        offset: int,
        options: QueryOptions,
    ) -> str:
        param_parts = []
        for p in view_def.parameters:
            param_parts.append(f"{p.name}={{{p.name}:{p.clickhouse_type}}}")

        param_str = ", ".join(param_parts)
        # Zero-parameter views are plain CH views, not parameterised table
        # functions: emitting 'db.view()' makes ClickHouse treat it as a table
        # FUNCTION call and reject it (Code 46 UNKNOWN_FUNCTION). Only append the
        # parens when there is at least one parameter to bind. (P1.2)
        if param_parts:
            view_call = f"{self._database}.{view_def.name}({param_str})"
        else:
            view_call = f"{self._database}.{view_def.name}"

        view_has_limit = any(p.name == "limit" for p in view_def.parameters)

        if view_has_limit and offset == 0 and not options.after_key:
            return f"SELECT * FROM {view_call}"

        if options.after_key is not None and options.order_by:
            order_col = self._validate_order_by(options.order_by)
            order_dir = options.order_dir or "asc"
            op = ">" if order_dir == "asc" else "<"
            key_type = _after_key_ch_type(options.after_key)
            return (
                f"SELECT * FROM {view_call} "
                f"WHERE {order_col} {op} {{_after_key:{key_type}}} "
                f"ORDER BY {order_col} {order_dir} "
                f"LIMIT {limit}"
            )

        if offset > 0:
            return f"SELECT * FROM {view_call} LIMIT {limit} OFFSET {offset}"

        return f"SELECT * FROM {view_call} LIMIT {limit}"

    def _validate_order_by(self, order_by: str) -> str:
        """Allowlist a keyset ``order_by`` to a single bare column identifier.

        The column is interpolated into the outer wrapper query (ClickHouse cannot
        bind an identifier), so a free string is a SQL-injection sink. Reject
        anything that is not one unquoted column identifier (F-QUERY-ORDERBY).
        """
        if not _SAFE_ORDER_BY.match(order_by):
            raise ViewExecutionError(
                f"Invalid order_by {order_by!r}: keyset order_by must be a bare column identifier"
            )
        return order_by

    def _resolve_limit(self, options: QueryOptions) -> int:
        if options.limit is not None:
            return min(options.limit, self._max_limit)
        return self._default_limit

    def _resolve_timeout(self, options: QueryOptions) -> int:
        if options.timeout_seconds is not None:
            return min(options.timeout_seconds, self._max_timeout)
        return self._default_timeout
