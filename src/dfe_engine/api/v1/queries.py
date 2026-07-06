#  Project:      dfe-engine
#  File:         src/dfe_engine/api/v1/queries.py
#  Purpose:      REST API for query execution via parameterized views
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Queries router — parameterized view catalog + execution.

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

from dfe_engine.api.deps import CurrentUser, Settings, require_action
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
    return executor.get_namespaces()


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


# ── View execution ──────────────────────────────────────────


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
    _auth: None = Depends(require_action(scopes_dict["query_execute"])),
) -> QueryResponse:
    """Execute a parameterized view and return JSON results.

    The ``org_id`` parameter is always injected from the authenticated
    user's context — it cannot be overridden by the client.
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


# ── Raw query execution (absorbs query/endpoint.py) ────────


@router.post(
    "/raw",
    response_model=QueryResponse,
    dependencies=[Depends(require_action(scopes_dict["query_raw"]))],
)
async def execute_raw_query(
    request: RawQueryRequest,
    user: CurrentUser,
    settings: Settings,
    _auth: None = Depends(require_action(scopes_dict["query_raw"])),
) -> QueryResponse:
    """Execute a raw query against a registered datasource adapter.

    This is the lower-level query path — for ad-hoc queries against
    datasource adapters rather than parameterized views. Requires the
    admin-level ``query:raw`` permission (NOT the org-scoped ``query:execute``):
    it runs arbitrary SQL via a datasource adapter with no org_id injection, so
    it must stay off the tenant-scoped view path.
    """
    from dfe_engine.query.datasources import get_adapter
    from dfe_engine.settings import get_clickhouse_config

    # The clickhouse adapter binds the process-wide manager singleton, so it
    # must get the settings-derived config, never the localhost defaults.
    scheme = request.datasource.partition(":")[0]
    adapter_config = get_clickhouse_config(settings) if scheme == "clickhouse" else None

    try:
        adapter = get_adapter(request.datasource, config=adapter_config)
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
