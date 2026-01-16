"""
FastAPI endpoint for Query API.

Mount this router in your FastAPI application to expose the Query API.
"""

from __future__ import annotations

import time
from typing import Annotated

from fastapi import APIRouter, Header, Response

from dfe_engine.query.datasources import get_adapter
from dfe_engine.query.models import QueryRequest
from dfe_engine.query.result import QueryResult

router = APIRouter(prefix="/api/v1", tags=["query"])


@router.post(
    "/query",
    response_class=Response,
    responses={
        200: {
            "content": {"application/vnd.apache.arrow.stream": {}},
            "description": "Arrow IPC stream with query results",
        },
    },
)
async def execute_query(
    request: QueryRequest,
    accept: Annotated[str, Header()] = "application/vnd.apache.arrow.stream",
) -> Response:
    """
    Execute a query against any registered datasource.

    Returns Arrow IPC stream with results. EXPLAIN plan (if requested) is
    embedded in the Arrow schema metadata.

    Headers returned:
    - X-Row-Count: Number of rows in result
    - X-Query-Duration-Ms: Query execution time
    - X-Explain-Duration-Ms: EXPLAIN execution time (if requested)
    - X-Truncated: Whether results were truncated
    - X-Cached: Whether results came from cache
    """
    adapter = get_adapter(request.datasource)

    options = request.options or {}
    timeout = options.timeout_seconds if hasattr(options, "timeout_seconds") else 30
    include_explain = (
        options.include_explain if hasattr(options, "include_explain") else False
    )
    parallel = options.parallel if hasattr(options, "parallel") else False

    start = time.perf_counter()

    if include_explain:
        table, explain = adapter.execute_with_explain(
            request.query,
            request.params,
            timeout,
            parallel=parallel,
        )
    else:
        table = adapter.execute(request.query, request.params, timeout)
        explain = None

    duration_ms = int((time.perf_counter() - start) * 1000)

    # Build QueryResult for serialization
    from dfe_engine.query.models import QueryMetadata

    metadata = QueryMetadata(
        row_count=table.num_rows,
        query_duration_ms=duration_ms,
        datasource=request.datasource,
    )

    result = QueryResult(table=table, metadata=metadata, explain=explain)

    # Serialize to Arrow IPC
    content = result.to_arrow_ipc(include_explain=include_explain)

    headers = {
        "X-Row-Count": str(table.num_rows),
        "X-Query-Duration-Ms": str(duration_ms),
        "X-Truncated": "false",
        "X-Cached": "false",
    }

    if explain:
        # Explain duration is part of total if parallel, otherwise separate
        headers["X-Explain-Duration-Ms"] = str(duration_ms if parallel else 0)

    return Response(
        content=content,
        media_type="application/vnd.apache.arrow.stream",
        headers=headers,
    )


@router.get("/query/health")
async def query_health() -> dict:
    """Health check for Query API."""
    from dfe_engine.query.datasources import list_adapters

    return {
        "status": "healthy",
        "adapters": list_adapters(),
    }
