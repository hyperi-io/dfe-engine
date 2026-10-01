#  Project:      dfe-engine
#  File:         src/dfe_engine/query/executor.py
#  Purpose:      Secure parameterized view executor with RBAC
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""ViewExecutor -- executes parameterized views via restricted ClickHouse connection.

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

from dfe_engine.clickhouse.quoting import BARE_REFERENCE, column_reference
from dfe_engine.query.catalog import RESERVED_PARAMS, ViewCatalog
from dfe_engine.query.models import (
    AuthContext,
    AuthorizationError,
    QueryMetadata,
    QueryOptions,
    ViewDefinition,
)
from dfe_engine.query.result import QueryResult

# ClickHouse's TYPE_MISMATCH, raised when a bound cursor does not parse as its column's type.
_TYPE_MISMATCH = re.compile(r"\bCode: 53\b|\bTYPE_MISMATCH\b")


class ViewExecutionError(Exception):
    """View execution failed."""


class ViewRequestError(ViewExecutionError):
    """The request's options cannot form a valid query; the caller must change them."""


def _cursor_text(value: Any, option: str = "after_key") -> str:
    """Return a keyset cursor value as the text bound to a ``String`` parameter.

    ClickHouse converts a String constant to the type of the column it is compared
    with, element by element inside a tuple, so one text binding pages a numeric,
    DateTime, Date, UUID or String column by that column's own ordering.

    Raises:
        ViewRequestError: If the value is not a string or a number.
    """
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        raise ViewRequestError(f"{option} must be a string or a number, not {type(value).__name__}")
    return str(value)


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
            if options.after_key is not None and _TYPE_MISMATCH.search(str(e)):
                raise ViewRequestError(
                    "after_key or after_tiebreak does not parse as its column's type "
                    f"(a timestamp is sent as e.g. '2024-01-15T12:00:00'): {e}"
                ) from e
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

    def _check_authorization(self, view_def: ViewDefinition, auth: AuthContext) -> None:
        from dfe_engine.auth import authorize
        from dfe_engine.settings import get_settings

        settings = get_settings()
        result = authorize(
            auth,
            "query:execute",
            view_def.label,
            enabled=settings.auth.enabled,
        )
        if not result.allowed:
            raise AuthorizationError(f"View '{view_def.label}': access denied ({result.reason})")

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
            final["limit"] = self._resolve_limit(options)
        if "time_from" in param_names and options.time_from:
            final["time_from"] = options.time_from
        if "time_to" in param_names and options.time_to:
            final["time_to"] = options.time_to
        if options.after_key is not None:
            final["_after_key"] = _cursor_text(options.after_key)
        if options.after_tiebreak is not None:
            final["_after_tie"] = _cursor_text(options.after_tiebreak, "after_tiebreak")

        return final

    def _build_sql(
        self,
        view_def: ViewDefinition,
        params: dict[str, Any],
        limit: int,
        offset: int,
        options: QueryOptions,
    ) -> str:
        self._check_paging(view_def, offset, options)
        param_parts = []
        for p in view_def.parameters:
            param_parts.append(f"{p.name}={{{p.name}:{p.clickhouse_type}}}")

        param_str = ", ".join(param_parts)
        view_call = f"{self._database}.{view_def.name}({param_str})"
        paging = f"LIMIT {limit} OFFSET {offset}" if offset > 0 else f"LIMIT {limit}"

        if options.order_by:
            direction = options.order_dir
            key = column_reference(options.order_by)
            columns = [key]
            cursors = ["{_after_key:String}"]
            if options.tiebreak_by:
                columns.append(column_reference(options.tiebreak_by))
                cursors.append("{_after_tie:String}")
            order = ", ".join(f"{column} {direction}" for column in columns)
            # The first keyset page sorts on the key too, or its last row is not the next cursor.
            where = ""
            if options.after_key is not None:
                op = ">" if direction == "asc" else "<"
                if len(columns) == 1:
                    where = f"WHERE {key} {op} {cursors[0]} "
                else:
                    where = f"WHERE ({', '.join(columns)}) {op} ({', '.join(cursors)}) "
            return f"SELECT * FROM {view_call} {where}ORDER BY {order} {paging}"

        if any(p.name == "limit" for p in view_def.parameters):
            return f"SELECT * FROM {view_call}"

        return f"SELECT * FROM {view_call} {paging}"

    @staticmethod
    def _check_paging(view_def: ViewDefinition, offset: int, options: QueryOptions) -> None:
        """Refuse paging options that cannot form a correct page.

        Raises:
            ViewRequestError: On a column that is not a plain name, or any combination
                that would page wrongly.
        """
        # Order columns are spliced into the SQL, since ClickHouse cannot bind an identifier.
        for option in ("order_by", "tiebreak_by"):
            name = getattr(options, option)
            if name and not BARE_REFERENCE.fullmatch(name):
                raise ViewRequestError(f"invalid {option} identifier: {name!r}")
        if options.tiebreak_by and not options.order_by:
            raise ViewRequestError("tiebreak_by needs order_by")
        if options.after_key is not None and not options.order_by:
            raise ViewRequestError("after_key needs order_by to name the column it pages on")
        if options.after_tiebreak is not None and options.after_key is None:
            raise ViewRequestError("after_tiebreak is sent together with after_key")
        if options.after_tiebreak is not None and not options.tiebreak_by:
            raise ViewRequestError("after_tiebreak needs tiebreak_by")
        if options.after_key is not None and options.tiebreak_by and options.after_tiebreak is None:
            raise ViewRequestError("with tiebreak_by set, after_key needs after_tiebreak")
        if options.after_key is not None and offset > 0:
            raise ViewRequestError("after_key and offset cannot be combined")
        pages = options.order_by or options.after_key is not None or offset > 0
        if pages and any(p.name == "limit" for p in view_def.parameters):
            raise ViewRequestError(
                f"view '{view_def.label}' caps its own rows with a limit parameter, so "
                "order_by, after_key and offset cannot page past its first rows; use limit"
            )

    def _resolve_limit(self, options: QueryOptions) -> int:
        if options.limit is not None:
            return min(options.limit, self._max_limit)
        return self._default_limit

    def _resolve_timeout(self, options: QueryOptions) -> int:
        if options.timeout_seconds is not None:
            return min(options.timeout_seconds, self._max_timeout)
        return self._default_timeout
