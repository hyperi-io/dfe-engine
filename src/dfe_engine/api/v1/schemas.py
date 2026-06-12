#  Project:      dfe-engine
#  File:         src/dfe_engine/api/v1/schemas.py
#  Purpose:      REST API for schema management and DDL generation
#  Language:     Python
#
#  License:      BUSL-1.1
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
from dfe_engine.api.pagination import (
    PaginatedResponse,
    PaginationParams,
    apply_schema_type_filter,
    apply_search,
    apply_sort,
)
from dfe_engine.auth.audit import audit_resource_change
from dfe_engine.schema.column_query import filter_columns
from dfe_engine.schema.models import (
    MetaSchema,
    MetaSchemaAddVersionRequest,
    MetaSchemaGetResponse,
    MetaSchemaUpdateRequest,
    PaginatedSchemaSummaryResponse,
    SchemaSummaryObject,
    SchemaVersionGet,
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
    search: str | None = Query(None, description="Search in path"),
    schema_type: list[str] | None = Query(
        None,
        description=(
            "Filter by top-level schema path segment (repeat param for multiple), "
            "e.g. meta, common-header, additional, hunt-results"
        ),
    ),
    sort_by: str | None = Query(None, description="Sort field (path, current, updated_at)"),
    sort_order: str = Query("asc", description="Sort order: asc/desc"),
):
    """List all meta schemas with optional filtering."""
    raw = registry.list_schemas()
    raw = apply_schema_type_filter(raw, schema_type)
    raw = apply_search(raw, search, ["path"])
    raw = apply_sort(raw, sort_by, sort_order)
    summaries = [
        SchemaSummaryObject(
            name=schema["path"],
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
    "/definitions/{schema_path:path}/versions/columns",
    response_model=MetaSchemaGetResponse,
    dependencies=[Depends(require_action("schema:read"))],
)
async def get_meta_schema(
    schema_path: str,
    user: CurrentUser,
    registry: SchemaReg,
    version: str = Query(..., description="Schema version to return (required)"),
    pagination: PaginationParams = Depends(),
    search: str | None = Query(
        None,
        description="Case-insensitive substring search across all column fields",
    ),
    name: str | None = Query(None, description="Filter by name (substring)"),
    type_filter: str | None = Query(
        None,
        alias="type",
        description="Filter by type (substring)",
    ),
    use_case: str | None = Query(None, description="Filter by use_case (substring)"),
    expr: str | None = Query(None, description="Filter by expr (substring)"),
    comment: str | None = Query(None, description="Filter by comment (substring)"),
    attribute: str | None = Query(None, description="Filter by attribute (substring)"),
    searchable_columns: list[str] | None = Query(
        None,
        description=(
            "Fields to apply ``search`` against (name, type, type_filter, use_case, "
            "expr, comment, attribute). Defaults to all column fields."
        ),
    ),
) -> MetaSchemaGetResponse:
    """Get one meta-schema definition by registry path (e.g. ``aws/cloudtrail``).

    Columns are paginated under ``version.columns``; use ``per_page=-1`` to return all
    matching columns (after search/filters) in one page.
    """
    from dfe_engine.schema.registry import (
        SchemaNotFoundError,
        SchemaValidationError,
        canonical_schema_path,
    )

    try:
        canonical_path = canonical_schema_path(schema_path)
    except SchemaValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "validation_error", "message": str(exc)},
        ) from exc

    try:
        meta = registry.get_schema(canonical_path)
    except SchemaNotFoundError:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "not_found",
                "message": f"Schema '{schema_path}' not found",
            },
        )
    if version not in meta.versions:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "not_found",
                "message": f"Version '{version}' not found for schema '{canonical_path}'",
            },
        )
    ver = meta.versions[version]
    version_ids = list(meta.versions.keys())
    try:
        filtered = filter_columns(
            ver.columns,
            search=search,
            name=name,
            type=type_filter,
            use_case=use_case,
            expr=expr,
            comment=comment,
            attribute=attribute,
            searchable_columns=searchable_columns,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "validation_error", "message": str(exc)},
        ) from exc
    columns_page = PaginatedResponse.from_list(
        filtered,
        pagination.page,
        pagination.per_page,
    )
    return MetaSchemaGetResponse(
        current=meta.current,
        selected=version,
        version=SchemaVersionGet(
            date=ver.date,
            type=ver.type,
            summary=ver.summary,
            columns=columns_page,
        ),
        path=canonical_path,
        versions=version_ids,
    )


@router.post(
    "/definitions/{schema_path:path}/versions",
    response_model=MetaSchema,
    dependencies=[Depends(require_action("schema:write"))],
    status_code=status.HTTP_201_CREATED,
)
async def add_meta_schema_version(
    schema_path: str,
    body: MetaSchemaAddVersionRequest,
    user: CurrentUser,
    registry: SchemaReg,
) -> MetaSchema:
    """Add a new meta-schema version (bumps semver from current and sets it current)."""
    from dfe_engine.schema.registry import (
        SchemaNotFoundError,
        SchemaValidationError,
        canonical_schema_path,
    )
    from dfe_engine.schema.schema_loader import SchemaLoadError
    from dfe_engine.schema.schema_manager import (
        SchemaManager,
        SchemaVersionError,
        next_version_for_type,
    )

    try:
        canonical_path = canonical_schema_path(schema_path)
    except SchemaValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "validation_error", "message": str(exc)},
        ) from exc

    if not body.columns:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "validation_error",
                "message": "columns must contain at least one column",
            },
        )

    try:
        current = registry.get_schema_current_version(canonical_path)
    except SchemaNotFoundError:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "not_found",
                "message": f"Schema '{schema_path}' not found",
            },
        ) from None
    except SchemaValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "validation_error", "message": str(exc)},
        ) from exc

    yaml_path = registry._yaml_path(canonical_path)
    version_type = body.type
    new_ver = next_version_for_type(current, version_type)

    try:
        if registry.schema_version_exists(canonical_path, new_ver):
            raise SchemaVersionError(f"Version '{new_ver}' already exists")
        col_dicts = [col.to_yaml_dict() for col in body.columns]
        SchemaManager.add_version(
            yaml_path,
            new_ver,
            col_dicts,
            type=version_type,
            summary=body.summary or "",
            set_current=True,
        )
    except SchemaVersionError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "validation_error", "message": str(exc)},
        ) from exc
    except SchemaLoadError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "schema_error", "message": str(exc)},
        ) from exc

    description = f"schema: {canonical_path} (add version {new_ver})"
    saved = registry.notify_schema_file_updated(
        canonical_path,
        description=description,
        created_by=user.user_id,
    )
    audit_resource_change(user.user_id, "meta_schema", canonical_path, "updated")
    return saved.model_copy(update={"path": canonical_path})


@router.post(
    "/definitions/{schema_path:path}",
    response_model=MetaSchema,
    dependencies=[Depends(require_action("schema:write"))],
    status_code=status.HTTP_201_CREATED,
)
async def create_meta_schema(
    schema_path: str,
    user: CurrentUser,
    registry: SchemaReg,
    body: MetaSchema,
) -> MetaSchema:
    """Create a new meta-schema at the given registry path (parent path + schema name)."""
    from dfe_engine.schema.registry import SchemaValidationError, canonical_schema_path
    from dfe_engine.schema.schema_manager import SchemaManager, SchemaVersionError

    try:
        canonical_path = canonical_schema_path(schema_path)
    except SchemaValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "validation_error", "message": str(exc)},
        ) from exc

    if body.path is not None:
        try:
            body_path = canonical_schema_path(body.path)
        except SchemaValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"code": "validation_error", "message": str(exc)},
            ) from exc
        if body_path != canonical_path:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "code": "path_mismatch",
                    "message": f"Body path {body.path!r} must match URL path {schema_path!r}",
                },
            )
    if registry.find_schema_at_location(canonical_path) is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "validation_error",
                "message": f"A meta-schema already exists at {canonical_path!r}",
            },
        )
    to_save = body.model_copy(update={"path": canonical_path})

    try:
        SchemaManager.validate_meta_schema_columns(to_save)
    except SchemaVersionError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "validation_error", "message": str(exc)},
        ) from exc

    try:
        saved = registry.save_schema(
            to_save,
            created_by=user.user_id,
        )
    except SchemaValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "validation_error", "message": str(exc)},
        ) from exc
    audit_resource_change(user.user_id, "meta_schema", canonical_path, "created")
    return saved.model_copy(update={"path": canonical_path})


@router.patch(
    "/definitions/{schema_path:path}",
    response_model=MetaSchema,
    dependencies=[Depends(require_action("schema:write"))],
)
async def update_meta_schema(
    schema_path: str,
    body: MetaSchemaUpdateRequest,
    user: CurrentUser,
    registry: SchemaReg,
    version: str | None = Query(
        None,
        description="Version to update summary for (required when summary is set)",
    ),
) -> MetaSchema:
    """Update meta-schema metadata: current pointer or a version summary."""
    from dfe_engine.schema.registry import (
        SchemaNotFoundError,
        SchemaValidationError,
        canonical_schema_path,
    )
    from dfe_engine.schema.schema_loader import SchemaLoadError
    from dfe_engine.schema.schema_manager import SchemaManager, SchemaVersionError

    try:
        canonical_path = canonical_schema_path(schema_path)
    except SchemaValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "validation_error", "message": str(exc)},
        ) from exc

    try:
        meta = registry.get_schema(canonical_path)
    except SchemaNotFoundError:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "not_found",
                "message": f"Schema '{schema_path}' not found",
            },
        )

    if body.summary is not None and not version:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "validation_error",
                "message": "Query parameter 'version' is required when updating summary",
            },
        )
    if version and version not in meta.versions:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "not_found",
                "message": f"Version '{version}' not found for schema '{canonical_path}'",
            },
        )

    yaml_path = registry._yaml_path(canonical_path)
    description_parts: list[str] = []

    try:
        if body.summary is not None:
            summary_version = version
            if summary_version is None:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail={
                        "code": "validation_error",
                        "message": "Query parameter 'version' is required when updating summary",
                    },
                )
            SchemaManager.update_version_summary(yaml_path, summary_version, body.summary)
            description_parts.append(f"update summary for {summary_version}")

        if body.current is not None:
            SchemaManager.set_current(yaml_path, body.current)
            description_parts.append(f"set current to {body.current}")
    except SchemaVersionError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "validation_error", "message": str(exc)},
        ) from exc
    except SchemaLoadError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "schema_error", "message": str(exc)},
        ) from exc

    description = f"schema: {canonical_path} ({', '.join(description_parts)})"
    saved = registry.notify_schema_file_updated(
        canonical_path,
        description=description,
        created_by=user.user_id,
    )
    audit_resource_change(user.user_id, "meta_schema", canonical_path, "updated")
    return saved.model_copy(update={"path": canonical_path})


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
