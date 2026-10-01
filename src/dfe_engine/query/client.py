#  Project:      dfe-engine
#  File:         src/dfe_engine/query/client.py
#  Purpose:      Query API client for consuming the DFE Query API
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Query API client.

Clients reference queries by label and pass parameters.
SQL is never exposed to clients -- it's resolved server-side
via ClickHouse parameterized views.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from dfe_engine.query.models import (
    AuthContext,
    QueryMetadata,
    QueryOptions,
)
from dfe_engine.query.result import QueryResult


class QueryClient:
    """Client for DFE Query API.

    Supports both direct (in-process) and HTTP modes.
    Clients specify query labels and parameters -- never raw SQL.
    """

    def __init__(
        self,
        base_url: str | None = None,
        direct: bool = False,
        timeout_seconds: int = 30,
    ):
        if not base_url and not direct:
            raise ValueError("Must specify base_url or direct=True")

        self.base_url = base_url
        self.direct = direct
        self.timeout_seconds = timeout_seconds
        self._http_client = None
        self._view_executor = None

    @property
    def http_client(self):
        """Lazy-load HTTP client."""
        if self._http_client is None:
            from scalo.http import HttpClient

            self._http_client = HttpClient(
                base_url=self.base_url,
                timeout=self.timeout_seconds + 10,
                retries=3,
            )
        return self._http_client

    def query(
        self,
        query_label: str,
        params: dict[str, Any] | None = None,
        *,
        limit: int | None = None,
        offset: int | None = None,
        order_by: str | None = None,
        order_dir: Literal["asc", "desc"] = "asc",
        after_key: str | int | float | None = None,
        tiebreak_by: str | None = None,
        after_tiebreak: str | int | float | None = None,
        time_from: str | None = None,
        time_to: str | None = None,
        timeout_seconds: int | None = None,
        store: str | None = None,
        cache: bool = True,
    ) -> QueryResult:
        """Execute query and return result.

        Keyset paging takes ``order_by`` and, from the second page, ``after_key``:
        the previous page's last ``order_by`` value as a string or a number. A
        non-unique key also takes ``tiebreak_by`` and ``after_tiebreak``.
        """
        options = QueryOptions(
            limit=limit,
            offset=offset,
            order_by=order_by,
            order_dir=order_dir,
            after_key=after_key,
            tiebreak_by=tiebreak_by,
            after_tiebreak=after_tiebreak,
            time_from=time_from,
            time_to=time_to,
            timeout_seconds=timeout_seconds,
            store=store,
            cache=cache,
        )

        return self._execute(query_label, params, options)

    def _execute(
        self,
        query_label: str,
        params: dict[str, Any] | None,
        options: QueryOptions,
    ) -> QueryResult:
        """Execute query via direct or HTTP mode."""
        if self.direct:
            return self._execute_direct(query_label, params, options)
        return self._execute_http(query_label, params, options)

    def _get_view_executor(self):
        """Get ViewExecutor, creating it lazily if possible."""
        if self._view_executor is None:
            try:
                from dfe_engine.query.catalog import ViewCatalog
                from dfe_engine.query.datasources.clickhouse import ClickHouseAdapter
                from dfe_engine.query.executor import ViewExecutor
                from dfe_engine.settings import get_settings

                settings = get_settings()
                qv = settings.query_views

                # `target` selects the connection (auth db); catalog/executor
                # qualify view + table lookups against the data database.
                adapter = ClickHouseAdapter(target=settings.clickhouse.database)
                restricted_client = adapter.get_restricted_client()
                admin_client = adapter.manager.get_clickhouse_client()
                data_db = settings.clickhouse.effective_data_database

                catalog = ViewCatalog(
                    client=admin_client,
                    database=data_db,
                    cache_ttl=qv.catalog_cache_ttl,
                    view_prefix=qv.view_prefix,
                )

                self._view_executor = ViewExecutor(
                    restricted_client=restricted_client,
                    catalog=catalog,
                    database=data_db,
                    default_limit=qv.default_limit,
                    max_limit=qv.max_limit,
                    default_timeout=qv.default_timeout,
                    max_timeout=qv.max_timeout,
                )
            except Exception:
                return None

        return self._view_executor

    def _execute_direct(
        self,
        query_label: str,
        params: dict[str, Any] | None,
        options: QueryOptions,
    ) -> QueryResult:
        """Execute query directly (in-process) via ViewExecutor."""
        executor = self._get_view_executor()
        if executor is None:
            raise RuntimeError(
                "ViewExecutor not available -- check ClickHouse connection "
                "and restricted user configuration"
            )

        auth = AuthContext(
            org_id="direct",
            user_id="direct",
            roles=["admin"],
            request_id=str(uuid.uuid4()),
        )

        return executor.execute(query_label, params, auth, options)

    def _execute_http(
        self,
        query_label: str,
        params: dict[str, Any] | None,
        options: QueryOptions,
    ) -> QueryResult:
        """Execute the labelled view through the view route, never the raw-SQL route."""
        response = self.http_client.post(
            f"/api/v1/queries/views/{query_label}/execute",
            json={
                "params": params or {},
                "options": options.model_dump(exclude_none=True),
            },
        )
        response.raise_for_status()

        data = response.json()
        rows = data.get("rows", [])
        metadata = QueryMetadata(
            row_count=int(data.get("row_count", len(rows))),
            query_duration_ms=int(data.get("query_duration_ms", 0)),
            query_label=query_label,
            datasource="clickhouse",
            request_id=data.get("request_id"),
            has_more=bool(data.get("has_more", False)),
            next_offset=data.get("next_offset"),
        )

        columns = data.get("columns", list(rows[0].keys()) if rows else [])
        return QueryResult(rows=rows, columns=columns, metadata=metadata)

    def close(self) -> None:
        """Close HTTP client."""
        if self._http_client:
            self._http_client.close()
            self._http_client = None

    def __enter__(self) -> QueryClient:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()
