#  Project:      dfe-engine
#  File:         src/dfe_engine/api/v1/schemas.py
#  Purpose:      REST API for schema management and DDL generation
#  Language:     Python
#
#  License:      FSL-1.1-ALv2
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Schemas router — schema versioning and DDL generation.

Wraps ``SchemaManager`` for version management and ``SchemaBuilderV2``
for DDL pipeline execution.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from dfe_engine.api.deps import CurrentUser, SourceReg, require_action
from dfe_engine.auth.audit import audit_resource_change
from dfe_engine.source.registry import SourceNotFoundError

router = APIRouter(prefix="/schemas", tags=["schemas"])


# ── Response models ─────────────────────────────────────────


class SchemaColumn(BaseModel):
    """A column in a schema definition."""

    name: str
    type: str
    use_case: str = ""
    attribute: str = ""
    description: str = ""


class SchemaVersionInfo(BaseModel):
    """Summary of a schema version."""

    version: str
    column_count: int
    columns: list[SchemaColumn]


class DDLResult(BaseModel):
    """Generated DDL output."""

    source_name: str
    create_table: str = Field(description="CREATE TABLE DDL")
    views: dict[str, str] = Field(default_factory=dict, description="View name → DDL")


class SchemaBuildResult(BaseModel):
    """Result of building a schema from a source."""

    source_name: str
    version: str = ""
    columns: list[SchemaColumn]
    ddl: DDLResult | None = None


# ── Endpoints ───────────────────────────────────────────────


@router.get("/{source_name}/columns", response_model=list[SchemaColumn])
async def get_schema_columns(
    source_name: str,
    request: Request,
    user: CurrentUser,
    registry: SourceReg,
    version: str | None = Query(None, description="Schema version (latest if not specified)"),
    _auth: None = Depends(require_action("source:read")),
) -> list[SchemaColumn]:
    """Get columns for a source's schema."""
    from dfe_engine.schema import SchemaLoader, SchemaLoadError

    try:
        source = registry.get_source(source_name)
    except SourceNotFoundError:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Source '{source_name}' not found"},
        )

    schema_path = getattr(source, "schema_path", None)
    if not schema_path:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "no_schema",
                "message": f"Source '{source_name}' has no schema file configured",
            },
        )

    try:
        columns = SchemaLoader.load_columns(schema_path, version=version)
    except SchemaLoadError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "schema_error", "message": str(exc)},
        )

    return [
        SchemaColumn(
            name=col.name,
            type=col.type,
            use_case=getattr(col, "use_case", "") or "",
            attribute=getattr(col, "attribute", "") or "",
            description=getattr(col, "description", "") or "",
        )
        for col in columns
    ]


@router.post("/{source_name}/build", response_model=SchemaBuildResult)
async def build_schema(
    source_name: str,
    request: Request,
    user: CurrentUser,
    registry: SourceReg,
    _auth: None = Depends(require_action("config:write")),
) -> SchemaBuildResult:
    """Build complete schema (DDL) from a source definition.

    Runs the v2 YAML → DDL pipeline and returns the generated DDL
    without executing it against ClickHouse.
    """
    from dfe_engine.schema import SchemaBuilderV2
    from dfe_engine.source.type_registry import TypeRegistry

    try:
        source = registry.get_source(source_name)
    except SourceNotFoundError:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Source '{source_name}' not found"},
        )

    try:
        type_registry = TypeRegistry()
        builder = SchemaBuilderV2(type_registry)
        result = builder.build(source)
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "build_error", "message": str(exc)},
        )

    columns = [
        SchemaColumn(
            name=col.name,
            type=col.type,
            use_case=getattr(col, "use_case", "") or "",
            attribute=getattr(col, "attribute", "") or "",
            description=getattr(col, "description", "") or "",
        )
        for col in result.columns
    ]

    ddl = None
    if result.create_table_ddl:
        ddl = DDLResult(
            source_name=source_name,
            create_table=result.create_table_ddl,
            views={k: v for k, v in (result.view_ddls or {}).items()},
        )

    audit_resource_change(user.user_id, "schema", source_name, "executed")
    return SchemaBuildResult(
        source_name=source_name,
        version=getattr(result, "version", "") or "",
        columns=columns,
        ddl=ddl,
    )
