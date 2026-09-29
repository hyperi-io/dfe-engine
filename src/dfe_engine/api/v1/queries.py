#  Project:      dfe-engine
#  File:         src/dfe_engine/api/v1/queries.py
#  Purpose:      REST API for query execution via parameterized views
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Queries router -- parameterized view catalog + execution.

Absorbs the functionality of ``query/endpoint.py`` and adds:
- View catalog browsing (namespaces, definitions, parameters)
- Authenticated view execution with org_id injection
- JSON response format
"""

from __future__ import annotations

import time
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from scalo.logger import logger

from dfe_engine.api.deps import CurrentUser, require_action
from dfe_engine.auth.rbac_scopes import scopes_dict
from dfe_engine.query.models import (
    QueryOptions,
    ViewDefinition,
    ViewExecuteRequest,
)

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


class QueryResponse(BaseModel):
    """JSON response for query execution."""

    rows: list[dict[str, Any]] = Field(description="Result rows")
    columns: list[str] = Field(description="Column names")
    row_count: int = Field(description="Number of rows returned")
    query_duration_ms: int = Field(description="Execution time in milliseconds")
    has_more: bool = Field(default=False, description="Whether more rows are available")
    next_offset: int | None = Field(default=None, description="Next offset for pagination")
    request_id: str | None = Field(default=None, description="Request correlation ID")


# -- Dependencies --------------------------------------------


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
                "message": "Query engine not configured -- ClickHouse connection required",
            },
        )
    return executor


ViewExec = Annotated[Any, Depends(_require_view_executor)]


# -- View catalog --------------------------------------------


@router.get(
    "/views",
    response_model=list[ViewDefinition],
    dependencies=[Depends(require_action(scopes_dict["query_read"]))],
)
async def list_views(
    user: CurrentUser,
    executor: ViewExec,
    namespace: str | None = Query(None, description="Filter by namespace"),
) -> list[ViewDefinition]:
    """List available parameterized views."""
    return executor.list_views(namespace=namespace)


@router.get(
    "/views/namespaces",
    response_model=list[str],
    dependencies=[Depends(require_action(scopes_dict["query_read"]))],
)
async def list_namespaces(
    user: CurrentUser,
    executor: ViewExec,
) -> list[str]:
    """List available view namespaces."""
    return executor._catalog.get_namespaces()


@router.get(
    "/views/{label:path}",
    response_model=ViewDefinition,
    dependencies=[Depends(require_action(scopes_dict["query_read"]))],
)
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


# -- View execution ------------------------------------------


@router.post(
    "/views/{label:path}/execute",
    response_model=QueryResponse,
    dependencies=[Depends(require_action(scopes_dict["query_execute"]))],
)
async def execute_view(
    label: str,
    body: ViewExecuteRequest,
    user: CurrentUser,
    executor: ViewExec,
) -> QueryResponse:
    """Execute a parameterized view and return JSON results.

    The ``org_id`` parameter is always injected from the authenticated
    user's context -- it cannot be overridden by the client.
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

    meta = result.metadata
    return QueryResponse(
        rows=result.rows,
        columns=result.columns,
        row_count=meta.row_count,
        query_duration_ms=meta.query_duration_ms,
        has_more=meta.has_more,
        next_offset=meta.next_offset,
        request_id=meta.request_id,
    )


# -- Raw query execution (absorbs query/endpoint.py) --------


@router.post(
    "/raw",
    response_model=QueryResponse,
    dependencies=[Depends(require_action(scopes_dict["raw_query_execute"]))],
)
async def execute_raw_query(
    request: RawQueryRequest,
    user: CurrentUser,
) -> QueryResponse:
    """Execute a raw query against a registered datasource adapter.

    This is the lower-level query path -- for ad-hoc queries against
    datasource adapters rather than parameterized views. The SQL runs as the
    engine's own ClickHouse user, so it requires ``raw_query:execute``, which no
    built-in role but ``admin`` holds, and ClickHouse runs it read-only.
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

    start = time.perf_counter()

    try:
        rows, columns = adapter.execute(request.query, request.params, timeout)
    except Exception as exc:
        logger.error("Raw query failed", query=request.query[:200], error=str(exc))
        raise HTTPException(
            status_code=500,
            detail={"code": "query_error", "message": str(exc)},
        )

    duration_ms = int((time.perf_counter() - start) * 1000)

    return QueryResponse(
        rows=rows,
        columns=columns,
        row_count=len(rows),
        query_duration_ms=duration_ms,
    )


# -- Query cost leaderboard (query_log_archive) -------------


class CostLeaderboardRow(BaseModel):
    """One cost consumer in the leaderboard (grouped by attribution id)."""

    id: str = Field(description="Attribution id (the hunt id for feature='hunts')")
    feature: str
    tenant_id: str = ""
    queries: int = Field(description="Number of queries")
    read_rows: int
    read_bytes: int
    duration_ms: int
    peak_memory: int


@router.get(
    "/cost-leaderboard",
    response_model=list[CostLeaderboardRow],
    dependencies=[Depends(require_action(scopes_dict["query_read"]))],
)
async def query_cost_leaderboard(
    user: CurrentUser,
    feature: str = Query("hunts", description="Attribution feature to rank (e.g. 'hunts')"),
    days: int = Query(7, ge=1, le=365, description="Lookback window in days"),
    limit: int = Query(50, ge=1, le=500, description="Max rows"),
) -> list[CostLeaderboardRow]:
    """Top query-cost consumers from ``dfe.query_log_archive``, heaviest first.

    Groups by the attribution id (the hunt id for feature='hunts') and returns the
    query count + summed read rows/bytes + duration + peak memory. Reads the MV the
    CH wrapper's log_comment attribution feeds - returns [] until it has data.
    """
    from dfe_engine.clickhouse import query_log_archive
    from dfe_engine.clickhouse.clickhouse_manager import ClickHouseManager
    from dfe_engine.settings import get_clickhouse_config, get_settings

    try:
        settings = get_settings()
        wrapper = ClickHouseManager.get_instance(
            get_clickhouse_config(settings=settings)
        ).get_clickhouse_client()
        rows = query_log_archive.cost_leaderboard(
            wrapper,
            feature=feature,
            days=days,
            limit=limit,
            database=settings.clickhouse.effective_data_database,
        )
    except Exception as exc:
        logger.error("cost leaderboard query failed", error=str(exc))
        raise HTTPException(
            status_code=503,
            detail={"code": "not_available", "message": f"cost leaderboard unavailable: {exc}"},
        )
    return [CostLeaderboardRow(**row) for row in rows]
