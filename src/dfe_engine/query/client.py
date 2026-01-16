"""
Query API client for consuming the DFE Query API.

Clients reference queries by label and pass parameters.
SQL is never exposed to clients - it's resolved server-side.
"""

from __future__ import annotations

import time
import uuid
from typing import TYPE_CHECKING, Any, Iterator

import pyarrow as pa

from dfe_engine.query.models import (
    AuthContext,
    QueryMetadata,
    QueryOptions,
)
from dfe_engine.query.result import QueryResult

if TYPE_CHECKING:
    import pandas as pd


class QueryClient:
    """
    Client for DFE Query API.

    Supports both direct (in-process) and HTTP modes.
    Clients specify query labels and parameters - never raw SQL.

    Examples:
        # HTTP mode (for external consumers)
        client = QueryClient(base_url="http://localhost:8000")

        # Direct mode (in-process, no HTTP)
        client = QueryClient(direct=True)

        # Execute query by label with parameters
        result = client.query(
            "analytics/user_activity",
            params={"event_types": ["login", "purchase"]},
            limit=500,
        )

        # Access data
        df = result.to_pandas()
        table = result.to_arrow()

        # With EXPLAIN plan
        result = client.query(
            "hunts/active_threats",
            params={"severities": ["critical"]},
            include_explain=True,
        )
        print(result.explain.steps)
    """

    def __init__(
        self,
        base_url: str | None = None,
        direct: bool = False,
        timeout_seconds: int = 30,
    ):
        """
        Initialize QueryClient.

        Args:
            base_url: API base URL for HTTP mode
            direct: Use direct in-process execution (no HTTP)
            timeout_seconds: Default query timeout
        """
        if not base_url and not direct:
            raise ValueError("Must specify base_url or direct=True")

        self.base_url = base_url
        self.direct = direct
        self.timeout_seconds = timeout_seconds
        self._http_client = None

    @property
    def http_client(self):
        """Lazy-load HTTP client."""
        if self._http_client is None:
            import httpx

            self._http_client = httpx.Client(
                base_url=self.base_url,
                timeout=self.timeout_seconds + 10,  # Buffer for network
            )
        return self._http_client

    def query(
        self,
        query_label: str,
        params: dict[str, Any] | None = None,
        *,
        limit: int | None = None,
        offset: int | None = None,
        time_from: str | None = None,
        time_to: str | None = None,
        timeout_seconds: int | None = None,
        store: str | None = None,
        cache: bool = True,
    ) -> QueryResult:
        """
        Execute query and return result.

        Args:
            query_label: Query label (e.g., 'analytics/user_activity')
            params: Query parameters (validated against query schema)
            limit: Max rows to return
            offset: Skip first N rows
            time_from: Start time (ISO8601) for time-bounded queries
            time_to: End time (ISO8601) for time-bounded queries
            timeout_seconds: Query timeout
            store: Target store (only if query allows store: '*')
            cache: Allow cached results

        Returns:
            QueryResult with data and metadata
        """
        options = QueryOptions(
            limit=limit,
            offset=offset,
            time_from=time_from,
            time_to=time_to,
            timeout_seconds=timeout_seconds,
            store=store,
            cache=cache,
            include_explain=False,
        )

        return self._execute(query_label, params, options)

    def query_df(
        self,
        query_label: str,
        params: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> pd.DataFrame:
        """
        Execute query and return pandas DataFrame.

        Zero-copy conversion from Arrow where possible.

        Args:
            query_label: Query label
            params: Query parameters
            **kwargs: Passed to query()

        Returns:
            pandas DataFrame
        """
        result = self.query(query_label, params, **kwargs)
        return result.to_pandas()

    def query_with_explain(
        self,
        query_label: str,
        params: dict[str, Any] | None = None,
        *,
        parallel: bool = True,
        **kwargs: Any,
    ) -> QueryResult:
        """
        Execute query and return results with EXPLAIN plan.

        Args:
            query_label: Query label
            params: Query parameters
            parallel: Execute query and EXPLAIN concurrently
            **kwargs: Passed to query()

        Returns:
            QueryResult with table, metadata, and explain plan
        """
        options = QueryOptions(
            limit=kwargs.get("limit"),
            offset=kwargs.get("offset"),
            time_from=kwargs.get("time_from"),
            time_to=kwargs.get("time_to"),
            timeout_seconds=kwargs.get("timeout_seconds"),
            store=kwargs.get("store"),
            cache=kwargs.get("cache", True),
            include_explain=True,
            explain_parallel=parallel,
        )

        return self._execute(query_label, params, options)

    def query_batches(
        self,
        query_label: str,
        params: dict[str, Any] | None = None,
        batch_size: int = 10_000,
        **kwargs: Any,
    ) -> Iterator[pa.RecordBatch]:
        """
        Stream query results in batches.

        Useful for large results that don't fit in memory.

        Args:
            query_label: Query label
            params: Query parameters
            batch_size: Maximum rows per batch
            **kwargs: Passed to query()

        Yields:
            Arrow RecordBatch
        """
        result = self.query(query_label, params, **kwargs)
        yield from result.iter_batches(batch_size)

    def _execute(
        self,
        query_label: str,
        params: dict[str, Any] | None,
        options: QueryOptions,
    ) -> QueryResult:
        """Execute query via direct or HTTP mode."""
        if self.direct:
            return self._execute_direct(query_label, params, options)
        else:
            return self._execute_http(query_label, params, options)

    def _execute_direct(
        self,
        query_label: str,
        params: dict[str, Any] | None,
        options: QueryOptions,
    ) -> QueryResult:
        """Execute query directly (in-process)."""
        from dfe_engine.query.datasources import get_adapter
        from dfe_engine.query.registry import get_registry
        from dfe_engine.query.validator import validate_params

        registry = get_registry()
        query_def = registry.get(query_label)

        # For direct mode, create a synthetic auth context
        # In production, this would come from the calling service's context
        auth = AuthContext(
            org_id="direct",  # Direct mode bypasses multi-tenancy
            user_id="direct",
            roles=["admin"],  # Direct mode has full access
            request_id=str(uuid.uuid4()),
        )

        # Validate and build parameters
        final_params = validate_params(query_def, params, options, auth)

        # Resolve store
        stores = registry.resolve_store(query_def, options.store)
        store = stores[0]  # For now, use first match

        # Render SQL
        sql = registry.render_sql(query_def, final_params, store)

        # Get adapter and execute
        adapter = get_adapter(query_def.datasource)

        start = time.perf_counter()

        timeout = final_params.get("timeout_seconds", self.timeout_seconds)

        if options.include_explain:
            table, explain = adapter.execute_with_explain(
                sql,
                final_params,
                timeout,
                parallel=options.explain_parallel,
            )
            explain_duration = int((time.perf_counter() - start) * 1000)
        else:
            table = adapter.execute(sql, final_params, timeout)
            explain = None
            explain_duration = None

        duration_ms = int((time.perf_counter() - start) * 1000)

        metadata = QueryMetadata(
            row_count=table.num_rows,
            query_duration_ms=duration_ms,
            query_label=query_label,
            datasource=query_def.datasource,
            store=store,
            explain_duration_ms=explain_duration,
            request_id=auth.request_id,
        )

        return QueryResult(table=table, metadata=metadata, explain=explain)

    def _execute_http(
        self,
        query_label: str,
        params: dict[str, Any] | None,
        options: QueryOptions,
    ) -> QueryResult:
        """Execute query via HTTP API."""
        response = self.http_client.post(
            "/api/v1/query",
            json={
                "query": query_label,
                "params": params,
                "options": options.model_dump(exclude_none=True),
            },
        )
        response.raise_for_status()

        # Parse metadata from headers
        metadata = QueryMetadata(
            row_count=int(response.headers.get("X-Row-Count", 0)),
            query_duration_ms=int(response.headers.get("X-Query-Duration-Ms", 0)),
            query_label=query_label,
            datasource=response.headers.get("X-Datasource", "unknown"),
            store=response.headers.get("X-Store"),
            truncated=response.headers.get("X-Truncated", "false").lower() == "true",
            cached=response.headers.get("X-Cached", "false").lower() == "true",
            explain_duration_ms=int(response.headers.get("X-Explain-Duration-Ms", 0))
            or None,
            request_id=response.headers.get("X-Request-Id"),
        )

        # Parse Arrow IPC response (explain embedded in schema metadata)
        return QueryResult.from_arrow_ipc(response.content, metadata)

    def close(self) -> None:
        """Close HTTP client."""
        if self._http_client:
            self._http_client.close()
            self._http_client = None

    def __enter__(self) -> QueryClient:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()
