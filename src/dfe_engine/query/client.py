"""
Query API client for consuming the DFE Query API.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Iterator

import pyarrow as pa
from pyarrow import ipc

from dfe_engine.query.models import ExplainPlan, QueryMetadata, QueryOptions
from dfe_engine.query.result import QueryResult

if TYPE_CHECKING:
    import pandas as pd


class QueryClient:
    """
    Client for DFE Query API.

    Supports both direct (in-process) and HTTP modes.

    Examples:
        # HTTP mode (for external consumers)
        client = QueryClient(base_url="http://localhost:8000")

        # Direct mode (in-process, no HTTP)
        client = QueryClient(direct=True)

        # Query and get Arrow Table
        table = client.query("clickhouse:default", "SELECT * FROM logs")

        # Query and get DataFrame
        df = client.query_df("clickhouse:default", "SELECT * FROM logs")

        # Query with EXPLAIN
        result = client.query_with_explain(
            "clickhouse:default",
            "SELECT * FROM logs",
            parallel=True  # Run query and EXPLAIN concurrently
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
        datasource: str,
        sql: str,
        params: dict[str, Any] | None = None,
        timeout_seconds: int | None = None,
    ) -> pa.Table:
        """
        Execute query and return Arrow Table.

        Args:
            datasource: Datasource URI (e.g., 'clickhouse:default')
            sql: Query string
            params: Optional query parameters
            timeout_seconds: Query timeout (uses default if not specified)

        Returns:
            PyArrow Table
        """
        result = self._execute(
            datasource=datasource,
            sql=sql,
            params=params,
            timeout_seconds=timeout_seconds or self.timeout_seconds,
            include_explain=False,
        )
        return result.table

    def query_df(
        self,
        datasource: str,
        sql: str,
        params: dict[str, Any] | None = None,
        timeout_seconds: int | None = None,
    ) -> pd.DataFrame:
        """
        Execute query and return pandas DataFrame.

        Zero-copy conversion from Arrow where possible.

        Args:
            datasource: Datasource URI
            sql: Query string
            params: Optional query parameters
            timeout_seconds: Query timeout

        Returns:
            pandas DataFrame
        """
        table = self.query(datasource, sql, params, timeout_seconds)
        return table.to_pandas()

    def query_with_explain(
        self,
        datasource: str,
        sql: str,
        params: dict[str, Any] | None = None,
        timeout_seconds: int | None = None,
        parallel: bool = True,
    ) -> QueryResult:
        """
        Execute query and return results with EXPLAIN plan.

        Args:
            datasource: Datasource URI
            sql: Query string
            params: Optional query parameters
            timeout_seconds: Query timeout
            parallel: Execute query and EXPLAIN concurrently

        Returns:
            QueryResult with table, metadata, and explain plan
        """
        return self._execute(
            datasource=datasource,
            sql=sql,
            params=params,
            timeout_seconds=timeout_seconds or self.timeout_seconds,
            include_explain=True,
            parallel=parallel,
        )

    def query_batches(
        self,
        datasource: str,
        sql: str,
        params: dict[str, Any] | None = None,
        batch_size: int = 10_000,
    ) -> Iterator[pa.RecordBatch]:
        """
        Stream query results in batches.

        Useful for large results that don't fit in memory.

        Args:
            datasource: Datasource URI
            sql: Query string
            params: Optional query parameters
            batch_size: Maximum rows per batch

        Yields:
            Arrow RecordBatch
        """
        result = self._execute(
            datasource=datasource,
            sql=sql,
            params=params,
            timeout_seconds=self.timeout_seconds,
            include_explain=False,
        )
        yield from result.iter_batches(batch_size)

    def _execute(
        self,
        datasource: str,
        sql: str,
        params: dict[str, Any] | None,
        timeout_seconds: int,
        include_explain: bool,
        parallel: bool = False,
    ) -> QueryResult:
        """Execute query via direct or HTTP mode."""
        if self.direct:
            return self._execute_direct(
                datasource, sql, params, timeout_seconds, include_explain, parallel
            )
        else:
            return self._execute_http(
                datasource, sql, params, timeout_seconds, include_explain, parallel
            )

    def _execute_direct(
        self,
        datasource: str,
        sql: str,
        params: dict[str, Any] | None,
        timeout_seconds: int,
        include_explain: bool,
        parallel: bool,
    ) -> QueryResult:
        """Execute query directly (in-process)."""
        from dfe_engine.query.datasources import get_adapter

        adapter = get_adapter(datasource)

        start = time.perf_counter()

        if include_explain:
            table, explain = adapter.execute_with_explain(
                sql, params, timeout_seconds, parallel=parallel
            )
            explain_duration = int((time.perf_counter() - start) * 1000)
        else:
            table = adapter.execute(sql, params, timeout_seconds)
            explain = None
            explain_duration = None

        duration_ms = int((time.perf_counter() - start) * 1000)

        metadata = QueryMetadata(
            row_count=table.num_rows,
            query_duration_ms=duration_ms,
            datasource=datasource,
            explain_duration_ms=explain_duration,
        )

        return QueryResult(table=table, metadata=metadata, explain=explain)

    def _execute_http(
        self,
        datasource: str,
        sql: str,
        params: dict[str, Any] | None,
        timeout_seconds: int,
        include_explain: bool,
        parallel: bool,
    ) -> QueryResult:
        """Execute query via HTTP API."""
        options = QueryOptions(
            timeout_seconds=timeout_seconds,
            include_explain=include_explain,
            parallel=parallel,
        )

        response = self.http_client.post(
            "/api/v1/query",
            json={
                "datasource": datasource,
                "query": sql,
                "params": params,
                "options": options.model_dump(),
            },
        )
        response.raise_for_status()

        # Parse metadata from headers
        metadata = QueryMetadata(
            row_count=int(response.headers.get("X-Row-Count", 0)),
            query_duration_ms=int(response.headers.get("X-Query-Duration-Ms", 0)),
            datasource=datasource,
            truncated=response.headers.get("X-Truncated", "false").lower() == "true",
            cached=response.headers.get("X-Cached", "false").lower() == "true",
            explain_duration_ms=int(response.headers.get("X-Explain-Duration-Ms", 0))
            or None,
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
