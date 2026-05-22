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

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile, status
from pydantic import BaseModel, Field

from dfe_engine.api.deps import CurrentUser, SchemaReg, SourceReg, require_action
from dfe_engine.api.errors import ErrorResponse
from dfe_engine.api.pagination import PaginationParams, apply_search, apply_sort
from dfe_engine.auth.audit import audit_resource_change
from dfe_engine.schema.models import (
    MetaSchema,
    PaginatedSchemaSummaryResponse,
    SchemaSummaryObject,
)
from dfe_engine.schema.models import (
    SchemaColumn as MetaSchemaColumn,
)
from dfe_engine.services.schema.elastic_schema_service import (
    ElasticSchemaConversionError,
    ElasticSchemaService,
)
from dfe_engine.settings import get_settings
from dfe_engine.source.registry import SourceNotFoundError

router = APIRouter(prefix="/schemas", tags=["schemas"])


def _meta_schema_location(schema_path: str) -> tuple[str, str]:
    """Split a registry key into parent path and schema name (YAML stem)."""
    parts = [p for p in schema_path.replace("\\", "/").split("/") if p]
    if not parts:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "validation_error", "message": "Invalid empty schema path"},
        )
    if len(parts) == 1:
        return "", parts[0]
    return "/".join(parts[:-1]), parts[-1]


def _existing_schema_at_location(registry: SchemaReg, schema_path: str) -> str | None:
    """Return registry key if another schema shares the same parent path and name."""
    parent, name = _meta_schema_location(schema_path)
    for row in registry.list_schemas():
        existing = row["path"]
        ep, en = _meta_schema_location(existing)
        if ep == parent and en == name:
            return existing
    return None


def _reject_body_over_limit_via_content_length(
    request: Request,
    max_payload_bytes: int,
    slack_bytes: int,
) -> None:
    raw = request.headers.get("content-length")
    if raw is None:
        return
    try:
        content_length = int(raw)
    except ValueError:
        return
    if content_length > max_payload_bytes + slack_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail={
                "code": "upload_too_large",
                "message": (
                    f"Request body exceeds maximum upload size "
                    f"({max_payload_bytes} bytes) for this endpoint"
                ),
            },
        )


async def _read_upload_capped(upload: UploadFile, *, max_bytes: int, read_chunk_size: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    chunk_cap = min(read_chunk_size, max_bytes + 1)
    while True:
        chunk = await upload.read(chunk_cap)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail={
                    "code": "upload_too_large",
                    "message": (
                        f"Upload exceeds maximum size ({max_bytes} bytes) for this endpoint"
                    ),
                },
            )
        chunks.append(chunk)
    return b"".join(chunks)


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
@router.get(
    "",
    response_model=PaginatedSchemaSummaryResponse,
    dependencies=[Depends(require_action("schema:read"))],
)
async def list_schemas(
    user: CurrentUser,
    registry: SchemaReg,
    pagination: PaginationParams = Depends(),
    search: str | None = Query(None, description="Search in path/description"),
    sort_by: str | None = Query(None, description="Sort field (path, description)"),
    sort_order: str = Query("asc", description="Sort order: asc/desc"),
):
    """List all meta schemas with optional filtering."""
    raw = registry.list_schemas()
    raw = apply_search(raw, search, ["path", "description"])
    raw = apply_sort(raw, sort_by, sort_order)
    summaries = [
        SchemaSummaryObject(
            name=schema["path"],
            description=schema["description"],
            current=schema["current"],
            versions=schema["versions"],
            updated_at=schema["updated_at"],
            column_count=schema["column_count"],
        )
        for schema in raw
    ]
    return PaginatedSchemaSummaryResponse.from_summaries(
        summaries, pagination.page, pagination.per_page
    )


@router.get(
    "/definitions/{schema_path:path}",
    response_model=MetaSchema,
    dependencies=[Depends(require_action("schema:read"))],
)
async def get_meta_schema(
    schema_path: str,
    user: CurrentUser,
    registry: SchemaReg,
) -> MetaSchema:
    """Get one meta-schema definition by registry path (e.g. ``aws/cloudtrail``)."""
    from dfe_engine.schema.registry import SchemaNotFoundError

    try:
        meta = registry.get_schema(schema_path)
    except SchemaNotFoundError:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "not_found",
                "message": f"Schema '{schema_path}' not found",
            },
        )
    return meta.model_copy(update={"path": schema_path})


@router.post(
    "/definitions/{schema_path:path}",
    response_model=MetaSchema,
    dependencies=[Depends(require_action("schema:write"))],
)
async def create_meta_schema(
    schema_path: str,
    user: CurrentUser,
    registry: SchemaReg,
    body: MetaSchema,
    description: str | None = Query(
        None,
        description="Optional git commit / change summary when the store is git-backed",
    ),
) -> MetaSchema:
    """Create a new meta-schema at the given registry path (parent path + schema name)."""
    from dfe_engine.schema.registry import SchemaValidationError

    if body.path is not None and body.path != schema_path:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "path_mismatch",
                "message": f"Body path {body.path!r} must match URL path {schema_path!r}",
            },
        )
    if _existing_schema_at_location(registry, schema_path) is not None:
        parent, name = _meta_schema_location(schema_path)
        loc = f"{parent}/{name}" if parent else name
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "validation_error",
                "message": f"A meta-schema already exists at {loc!r}",
            },
        )
    to_save = body.model_copy(update={"path": schema_path})

    try:
        saved = registry.save_schema(
            to_save,
            created_by=user.user_id,
            description=description,
        )
    except SchemaValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "validation_error", "message": str(exc)},
        ) from exc
    audit_resource_change(user.user_id, "meta_schema", schema_path, "created")
    return saved.model_copy(update={"path": schema_path})


@router.delete(
    "/definitions/{schema_path:path}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_action("schema:delete"))],
)
async def delete_meta_schema(
    schema_path: str,
    user: CurrentUser,
    registry: SchemaReg,
) -> None:
    """Delete a meta-schema definition by registry path."""
    from dfe_engine.schema.registry import SchemaNotFoundError, SchemaValidationError

    try:
        registry.get_schema(schema_path)
    except SchemaNotFoundError:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "not_found",
                "message": f"Schema '{schema_path}' not found",
            },
        )
    try:
        registry.delete_schema(schema_path)
    except SchemaValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "validation_error", "message": str(exc)},
        ) from exc
    audit_resource_change(user.user_id, "meta_schema", schema_path, "deleted")


@router.post(
    "/elastic-converter",
    response_model=list[MetaSchemaColumn],
    responses={
        413: {
            "model": ErrorResponse,
            "description": (
                "Upload or declared Content-Length exceeds api.elastic_converter_max_upload_bytes "
                "(HTTP 413, code upload_too_large). Tune via DFE_API_ELASTIC_CONVERTER_* env vars."
            ),
        },
    },
    dependencies=[Depends(require_action("schema:read"))],
)
async def elastic_converter(
    request: Request,
    user: CurrentUser,
    file: UploadFile = File(...),
) -> list[MetaSchemaColumn]:
    """Extract meta-schema columns from an Elasticsearch index template JSON file.

    Accepts Beat-style exports with ``template.mappings.properties`` or API-style
    ``mappings.properties``. Column layout follows curated YAML conventions
    (snake_case ``name``, ``@source:`` dotted ``expr``).

    Upload size is capped by ``api.elastic_converter_max_upload_bytes`` (reject with 413 and
    ``upload_too_large`` when exceeded). Override with ``DFE_API_ELASTIC_CONVERTER_*``.
    """
    api_s = get_settings().api
    max_bytes = api_s.elastic_converter_max_upload_bytes
    slack = api_s.elastic_converter_content_length_slack_bytes
    chunk_sz = api_s.elastic_converter_read_chunk_size
    _reject_body_over_limit_via_content_length(request, max_bytes, slack)

    try:
        raw = await _read_upload_capped(file, max_bytes=max_bytes, read_chunk_size=chunk_sz)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "upload_read_error", "message": str(exc)},
        ) from exc
    try:
        return ElasticSchemaService.template_json_to_columns(raw)
    except ElasticSchemaConversionError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "elastic_convert_error", "message": str(exc)},
        ) from exc


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
