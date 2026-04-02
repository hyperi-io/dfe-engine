#  Project:      dfe-engine
#  File:         src/dfe_engine/api/v1/queries.py
#  Purpose:      REST API for query execution via parameterized views
#  Language:     Python
#
#  License:      FSL-1.1-ALv2
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Queries router — parameterized view catalog + execution.

Absorbs the functionality of ``query/endpoint.py`` and adds:
- View catalog browsing (namespaces, definitions, parameters)
- Authenticated view execution with org_id injection
- Arrow IPC response format with metadata headers
"""

from __future__ import annotations

import time
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from hyperi_pylib.logger import logger
from pydantic import BaseModel, Field

from dfe_engine.api.deps import CurrentUser, require_action
from dfe_engine.query.models import (
    QueryMetadata,
    QueryOptions,
    ViewDefinition,
    ViewExecuteRequest,
)
from dfe_engine.query.result import QueryResult

router = APIRouter(prefix="/queries", tags=["queries"])


class RawQueryRequest(BaseModel):
    """Request for raw query execution against a datasource adapter."""

    datasource: str = Field(
        ...,
        description="Datasource URI (e.g. 'clickhouse:default')",
        examples=["clickhouse:default"],
    )
    query: str = Field(
        ...,
        description="Query label or identifier",
    )
    params: dict[str, Any] | None = Field(
        default=None,
        description="Query parameters",
    )
    options: QueryOptions | None = Field(
        default=None,
        description="Execution options (limit, timeout, etc.)",
    )


# ── Dependencies ────────────────────────────────────────────


def _get_view_executor(request: Request):
    """Resolve ViewExecutor from app state. Returns None if not configured."""
    return getattr(request.app.state, "view_executor", None)


def _require_view_executor(request: Request):
    """Resolve ViewExecutor, raising 503 if not configured."""
    executor = _get_view_executor(request)
    if executor is None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "not_configured",
                "message": "Query engine not configured — ClickHouse connection required",
            },
        )
    return executor


ViewExec = Annotated[Any, Depends(_require_view_executor)]


# ── View catalog ────────────────────────────────────────────


@router.get("/views", response_model=list[ViewDefinition])
async def list_views(
    user: CurrentUser,
    executor: ViewExec,
    namespace: str | None = Query(None, description="Filter by namespace"),
) -> list[ViewDefinition]:
    """List available parameterized views."""
    return executor.list_views(namespace=namespace)


@router.get("/views/namespaces", response_model=list[str])
async def list_namespaces(
    user: CurrentUser,
    executor: ViewExec,
) -> list[str]:
    """List available view namespaces."""
    return executor._catalog.get_namespaces()


@router.get("/views/{label:path}", response_model=ViewDefinition)
async def get_view(
    label: str,
    user: CurrentUser,
    executor: ViewExec,
) -> ViewDefinition:
    """Get a specific view definition with parameters and metadata."""
    try:
        return executor.get_view(label)
    except KeyError:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"View '{label}' not found"},
        )


# ── View execution ──────────────────────────────────────────


@router.post(
    "/views/{label:path}/execute",
    response_class=Response,
    responses={
        200: {
            "content": {"application/vnd.apache.arrow.stream": {}},
            "description": "Arrow IPC stream with query results",
        },
    },
)
async def execute_view(
    label: str,
    body: ViewExecuteRequest,
    user: CurrentUser,
    executor: ViewExec,
    _auth: None = Depends(require_action("query:execute")),
) -> Response:
    """Execute a parameterized view and return Arrow IPC results.

    The ``org_id`` parameter is always injected from the authenticated
    user's context — it cannot be overridden by the client.

    Response headers:
    - ``X-Row-Count``: Number of rows returned
    - ``X-Query-Duration-Ms``: Execution time in milliseconds
    - ``X-Has-More``: Whether more rows are available
    - ``X-Next-Offset``: Next offset for pagination (if applicable)
    - ``X-Request-ID``: Request correlation ID
    """
    from dfe_engine.query.executor import ViewExecutionError

    try:
        result = executor.execute(
            label=label,
            params=body.params,
            auth=user,
            options=body.options,
        )
    except KeyError:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"View '{label}' not found"},
        )
    except ViewExecutionError as exc:
        logger.error("View execution failed", label=label, error=str(exc))
        raise HTTPException(
            status_code=500,
            detail={"code": "query_error", "message": str(exc)},
        )

    content = result.to_arrow_ipc()
    meta = result.metadata

    headers = {
        "X-Row-Count": str(meta.row_count),
        "X-Query-Duration-Ms": str(meta.query_duration_ms),
        "X-Has-More": str(meta.has_more).lower(),
    }
    if meta.next_offset is not None:
        headers["X-Next-Offset"] = str(meta.next_offset)
    if meta.request_id:
        headers["X-Request-ID"] = meta.request_id

    return Response(
        content=content,
        media_type="application/vnd.apache.arrow.stream",
        headers=headers,
    )


# ── Raw query execution (absorbs query/endpoint.py) ────────


@router.post(
    "/raw",
    response_class=Response,
    responses={
        200: {
            "content": {"application/vnd.apache.arrow.stream": {}},
            "description": "Arrow IPC stream with query results",
        },
    },
)
async def execute_raw_query(
    request: RawQueryRequest,
    user: CurrentUser,
    _auth: None = Depends(require_action("query:execute")),
    accept: Annotated[str, Header()] = "application/vnd.apache.arrow.stream",
) -> Response:
    """Execute a raw query against a registered datasource adapter.

    This is the lower-level query path — for ad-hoc queries against
    datasource adapters rather than parameterized views. Requires
    ``query:execute`` permission.

    Response headers:
    - ``X-Row-Count``: Number of rows returned
    - ``X-Query-Duration-Ms``: Execution time in milliseconds
    - ``X-Truncated``: Whether results were truncated
    - ``X-Cached``: Whether results came from cache
    """
    from dfe_engine.query.datasources import get_adapter

    try:
        adapter = get_adapter(request.datasource)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_datasource", "message": str(exc)},
        )

    options = request.options or QueryOptions()
    timeout = options.timeout_seconds or 30
    include_explain = options.include_explain

    start = time.perf_counter()

    try:
        if include_explain:
            table, explain = adapter.execute_with_explain(
                request.query,
                request.params,
                timeout,
                parallel=options.explain_parallel,
            )
        else:
            table = adapter.execute(request.query, request.params, timeout)
            explain = None
    except Exception as exc:
        logger.error("Raw query failed", query=request.query[:200], error=str(exc))
        raise HTTPException(
            status_code=500,
            detail={"code": "query_error", "message": str(exc)},
        )

    duration_ms = int((time.perf_counter() - start) * 1000)

    metadata = QueryMetadata(
        row_count=table.num_rows,
        query_duration_ms=duration_ms,
        query_label=request.query,
        datasource=request.datasource,
    )

    result = QueryResult(table=table, metadata=metadata, explain=explain)
    content = result.to_arrow_ipc(include_explain=include_explain)

    headers = {
        "X-Row-Count": str(table.num_rows),
        "X-Query-Duration-Ms": str(duration_ms),
        "X-Truncated": "false",
        "X-Cached": "false",
    }

    return Response(
        content=content,
        media_type="application/vnd.apache.arrow.stream",
        headers=headers,
    )


# ── Health ──────────────────────────────────────────────────


@router.get("/health")
async def query_health() -> dict:
    """Health check for query engine."""
    from dfe_engine.query.datasources import list_adapters

    return {
        "status": "healthy",
        "adapters": list_adapters(),
    }
