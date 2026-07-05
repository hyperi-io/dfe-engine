#  Project:      dfe-engine
#  File:         src/dfe_engine/api/v1/discovery.py
#  Purpose:      REST API for ClickHouse table/schema exploration
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Discovery router — ClickHouse table and column introspection.

Provides REST access to database metadata without needing direct
ClickHouse connectivity from the UI. Requires a configured
ClickHouse connection via ConnectionRegistry.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from dfe_engine.api.deps import CurrentUser, TenantClient, require_action
from dfe_engine.auth.rbac_scopes import scopes_dict

router = APIRouter(prefix="/discovery", tags=["discovery"])


# ── Response models ─────────────────────────────────────────


class TableInfo(BaseModel):
    """Summary of a ClickHouse table."""

    name: str
    database: str
    engine: str = ""
    total_rows: int | None = None
    total_bytes: int | None = None
    comment: str = ""


class ColumnInfo(BaseModel):
    """A column in a ClickHouse table."""

    name: str
    type: str
    default_kind: str = ""
    default_expression: str = ""
    comment: str = ""


class DatabaseInfo(BaseModel):
    """Summary of a ClickHouse database."""

    name: str
    engine: str = ""


# ── Endpoints ───────────────────────────────────────────────
#
# The CH client is the acting user's privilege-appropriate fixed user (via the
# TenantClient dependency), NOT a hardcoded admin ``default`` connection: an
# org_analyst lists only the tables their row-filtered reader can see, and an
# admin stays unrestricted. Discovery reads ``system.*`` metadata (no ``_org_id``,
# so no row policy applies) - CH still scopes system.tables/columns visibility to
# the objects the calling CH user has some grant on, which IS the least-privilege
# win here. The dependency raises 503 when the registry is unset or CH is down.


@router.get(
    "/databases",
    response_model=list[DatabaseInfo],
    dependencies=[Depends(require_action(scopes_dict["discovery_read"]))],
)
async def list_databases(
    user: CurrentUser,
    ch: TenantClient,
    _auth: None = Depends(require_action(scopes_dict["discovery_read"])),
) -> list[DatabaseInfo]:
    """List all ClickHouse databases."""
    try:
        result = ch.query("SELECT name, engine FROM system.databases ORDER BY name")
        return [
            DatabaseInfo(name=row[0], engine=row[1])
            for row in result.result_rows
            if row[0] not in ("system", "information_schema", "INFORMATION_SCHEMA")
        ]
    except Exception as exc:
        raise HTTPException(status_code=500, detail={"code": "query_error", "message": str(exc)})


@router.get(
    "/tables",
    response_model=list[TableInfo],
    dependencies=[Depends(require_action(scopes_dict["discovery_read"]))],
)
async def list_tables(
    user: CurrentUser,
    ch: TenantClient,
    database: str = Query("default", description="Database to list tables from"),
    engine: str | None = Query(None, description="Filter by engine type"),
    _auth: None = Depends(require_action(scopes_dict["discovery_read"])),
) -> list[TableInfo]:
    """List tables in a ClickHouse database."""
    try:
        query = (
            "SELECT name, database, engine, total_rows, total_bytes, comment "
            "FROM system.tables "
            "WHERE database = {db:String}"
        )
        params: dict[str, Any] = {"db": database}
        if engine:
            query += " AND engine = {eng:String}"
            params["eng"] = engine
        query += " ORDER BY name"

        result = ch.query(query, parameters=params)
        return [
            TableInfo(
                name=row[0],
                database=row[1],
                engine=row[2],
                total_rows=row[3],
                total_bytes=row[4],
                comment=row[5] or "",
            )
            for row in result.result_rows
        ]
    except Exception as exc:
        raise HTTPException(status_code=500, detail={"code": "query_error", "message": str(exc)})


@router.get(
    "/tables/{table_name}/columns",
    response_model=list[ColumnInfo],
    dependencies=[Depends(require_action(scopes_dict["discovery_read"]))],
)
async def list_columns(
    table_name: str,
    user: CurrentUser,
    ch: TenantClient,
    database: str = Query("default", description="Database containing the table"),
    _auth: None = Depends(require_action(scopes_dict["discovery_read"])),
) -> list[ColumnInfo]:
    """List columns for a specific table."""
    try:
        result = ch.query(
            "SELECT name, type, default_kind, default_expression, comment "
            "FROM system.columns "
            "WHERE database = {db:String} AND table = {tbl:String} "
            "ORDER BY position",
            parameters={"db": database, "tbl": table_name},
        )
        if not result.result_rows:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "not_found",
                    "message": f"Table '{database}.{table_name}' not found",
                },
            )
        return [
            ColumnInfo(
                name=row[0],
                type=row[1],
                default_kind=row[2] or "",
                default_expression=row[3] or "",
                comment=row[4] or "",
            )
            for row in result.result_rows
        ]
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail={"code": "query_error", "message": str(exc)})
