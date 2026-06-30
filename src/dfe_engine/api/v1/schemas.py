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

from typing import Any, Literal

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile, status
from pydantic import BaseModel, Field, model_validator

from dfe_engine.api.deps import (
    ClickHouseClient,
    CurrentUser,
    SchemaReg,
    SourceReg,
    require_action,
)
from dfe_engine.api.errors import ErrorResponse
from dfe_engine.api.pagination import (
    PaginatedResponse,
    PaginationParams,
    apply_schema_type_filter,
    apply_search,
    apply_sort,
)
from dfe_engine.auth.audit import audit_resource_change
from dfe_engine.auth.rbac_scopes import scopes_dict
from dfe_engine.git_identity import git_author
from dfe_engine.schema.column_query import filter_columns
from dfe_engine.schema.models import (
    MetaSchema,
    MetaSchemaAddVersionRequest,
    MetaSchemaGetResponse,
    MetaSchemaUpdateRequest,
    MetaSchemaVersionWriteResponse,
    PaginatedSchemaSummaryResponse,
    SchemaSummaryObject,
    SchemaVersionGet,
    meta_schema_version_write_response,
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


# ── JSON field promotion models ─────────────────────────────


class DraftColumn(BaseModel):
    """A ready-to-send meta-schema column derived from a discovered JSON path.

    Shaped to drop straight into ``MetaSchemaAddVersionRequest.columns`` (the
    create-version endpoint) or a new meta-schema's version -- the UI sends these
    verbatim, no field mapping. ``type`` is derived from the first observed
    ClickHouse type; for a multi-type path (``is_consistent=false``) confirm it
    before saving.
    """

    name: str = Field(
        description=(
            "Server-derived snake_case column name for the path. camelCase is split, "
            "dots and non-identifier characters become underscores, a leading digit is "
            "prefixed with 'f_', and a numeric suffix ('_2', '_3', ...) is added to avoid "
            "colliding with an existing column name."
        )
    )
    type: str = Field(
        description=(
            "DFE primitive type mapped from the first observed ClickHouse type "
            "(e.g. string, integer, float, boolean, datetime, date, uuid, ip; compound or "
            "unknown types fall back to 'json'). For a multi-type path this is a best-effort "
            "guess from the first type -- check the enclosing 'is_consistent' before saving."
        )
    )
    attribute: list[str] = Field(
        default_factory=list,
        description=(
            "ClickHouse storage attributes for the column (e.g. 'nullable', "
            "'lowcardinality'). Empty for a plain column."
        ),
    )
    use_case: str | None = Field(
        default=None,
        description=(
            "Index use case to generate for the column (dimension, range, bloom, "
            "fulltext, text_search), or null for no index. Always null on a discovered "
            "draft -- set it in the editor if you want an index."
        ),
    )
    expr: str = Field(
        description=(
            "DFE directive used as the column's expression. A '@copy: _json.<path>' "
            "directive tells dfe-loader to copy the value forward from the _json column "
            "on ingest (e.g. '@copy: _json.user.email')."
        )
    )
    comment: str | None = Field(
        default=None, description="Human-readable column description (defaults to the source path)."
    )


class JsonPathInfo(BaseModel):
    """One JSON path discovered inside a source's ``_json`` column."""

    path: str = Field(
        description="Dotted path to the field inside the _json column (e.g. 'user.email')."
    )
    types: list[str] = Field(
        description=(
            "Distinct ClickHouse types observed for this path across the scanned rows, in "
            "first-seen order. More than one entry means the field is stored as different "
            "types in different rows (e.g. ['Int64', 'String'])."
        )
    )
    is_consistent: bool = Field(
        description=(
            "True when the path has exactly one observed ClickHouse type (len(types) == 1). "
            "False means the type varies row to row, so the derived 'column.type' is only a "
            "best-effort guess from the first type and should be confirmed before promoting."
        )
    )
    promoted_to: str | None = Field(
        default=None,
        description=(
            "Name of the existing meta-schema column this path is already copied into (via a "
            "'@copy' directive), or null if not yet promoted. Only ever populated when "
            "discovering against a source that already has a meta_schema; always null while "
            "discovering against the catch-all landing table."
        ),
    )
    column: DraftColumn = Field(
        description=(
            "Ready-to-send meta-schema column derived from this path. Post it verbatim to the "
            "create-meta-schema-version endpoint (no client-side type mapping needed)."
        )
    )
    coverage_pct: float | None = Field(
        default=None,
        description=(
            "Percentage of scanned rows (0-100) in which this path is present and non-null. "
            "Only populated with '?stats=true'. Low coverage (e.g. 0.5%) flags a rare or "
            "optional field you may not want to promote."
        ),
    )
    distinct_count: int | None = Field(
        default=None,
        description=(
            "Approximate number of distinct values for this path -- a HyperLogLog estimate "
            "(uniqHLL12), not an exact count. Only populated with '?stats=true'. Useful for "
            "gauging cardinality, e.g. when choosing an index type."
        ),
    )
    samples: list[str] | None = Field(
        default=None,
        description=(
            "Up to N random, distinct example values for this path, rendered as strings. "
            "Only populated with '?samples=N'."
        ),
    )


class JsonPathsResponse(BaseModel):
    """Discovered JSON paths for a source."""

    source_name: str = Field(description="The source these paths were discovered for.")
    table: str = Field(
        description=(
            "Fully-qualified ClickHouse table actually queried ('db.table'). The source's "
            "own table when the selected version has a meta_schema, otherwise the shared "
            "catch-all landing table."
        )
    )
    json_column: str = Field(description="Name of the JSON column inspected (always '_json').")
    paths: list[JsonPathInfo] = Field(
        description="One entry per distinct JSON path found in the column."
    )


class SampleRowsResponse(BaseModel):
    """Random sample rows for a source, scoped to its match rule."""

    source_name: str = Field(description="The source these rows were sampled for.")
    table: str = Field(
        description=(
            "Fully-qualified ClickHouse table actually sampled ('db.table'). The source's "
            "own table when the selected version has a meta_schema, otherwise the shared "
            "catch-all landing table."
        )
    )
    match_field: str | None = Field(
        default=None,
        description=(
            "Match field rows were filtered on, or null when the source owns its own table "
            "(whole-table sample)."
        ),
    )
    match_value: str | None = Field(
        default=None,
        description="Match value rows were filtered on, or null for a whole-table sample.",
    )
    columns: list[str] = Field(description="Column names present in the sampled rows.")
    rows: list[dict[str, Any]] = Field(
        description="Sampled rows, each a column-name -> value mapping. Empty when nothing matched."
    )


class PromoteFieldRequest(BaseModel):
    """Promote one or more JSON paths into dedicated typed columns."""

    json_path: str | list[str] = Field(
        description="JSON path (e.g. user.email) or a list of paths for batch promotion",
    )
    column_name: str | None = Field(
        default=None,
        description="Target column name (single promotion only; batch uses suggested names)",
    )
    data_type: str | None = Field(
        default=None,
        description="DFE primitive override; auto-derived from the JSON type when omitted",
    )
    index_type: Literal["minmax", "set", "bloom_filter", "tokenbf_v1", "ngrambf_v1"] | None = Field(
        default=None,
        description="Optional ClickHouse secondary index family",
    )
    atomic: bool = Field(
        default=True,
        description="When true, all paths succeed or none commit; else best-effort",
    )

    @model_validator(mode="after")
    def _validate_shape(self) -> PromoteFieldRequest:
        if isinstance(self.json_path, list):
            if not self.json_path:
                raise ValueError("json_path list must not be empty")
            if self.column_name is not None:
                raise ValueError("column_name cannot be set for batch promotion")
        return self


class PromoteResult(BaseModel):
    """Per-path outcome of a promotion."""

    json_path: str
    status: Literal["ok", "error"]
    column_name: str | None = None
    data_type: str | None = None
    index_type: str | None = None
    copy_cel: str | None = None
    error: str | None = None


class SchemaDiff(BaseModel):
    """Proposed schema change returned by ``?dry_run=true``."""

    new_columns: list[SchemaColumn]
    ddl: list[str] = Field(default_factory=list, description="ALTER statements that would run")
    copy_directives: list[str] = Field(default_factory=list)


class PromoteFieldResponse(BaseModel):
    """Result of a promote-field call."""

    source_name: str
    schema_version: str | None = Field(
        default=None, description="New schema version, or null on dry_run / no commit"
    )
    results: list[PromoteResult]
    diff: SchemaDiff | None = Field(default=None, description="Populated only when dry_run=true")


# ── Endpoints ───────────────────────────────────────────────
@router.get(
    "",
    response_model=PaginatedSchemaSummaryResponse,
    dependencies=[Depends(require_action(scopes_dict["schema_read"]))],
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
    dependencies=[Depends(require_action(scopes_dict["schema_read"]))],
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
                "message": f"Schema {schema_path!r} not found",
            },
        )
    if version not in meta.versions:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "not_found",
                "message": f"Version {version!r} not found for schema {canonical_path!r}",
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
    response_model=MetaSchemaVersionWriteResponse,
    dependencies=[Depends(require_action(scopes_dict["schema_write"]))],
    status_code=status.HTTP_201_CREATED,
)
async def add_meta_schema_version(
    schema_path: str,
    body: MetaSchemaAddVersionRequest,
    user: CurrentUser,
    registry: SchemaReg,
) -> MetaSchemaVersionWriteResponse:
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
                "message": f"Schema {schema_path!r} not found",
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
            raise SchemaVersionError(f"Version {new_ver!r} already exists")
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
        created_by=git_author(user),
    )
    audit_resource_change(user.user_id, "meta_schema", canonical_path, "updated")
    return meta_schema_version_write_response(saved, path=canonical_path)


@router.post(
    "/definitions/{schema_path:path}",
    response_model=MetaSchema,
    dependencies=[Depends(require_action(scopes_dict["schema_write"]))],
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
            created_by=git_author(user),
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
    response_model=MetaSchemaVersionWriteResponse,
    dependencies=[Depends(require_action(scopes_dict["schema_write"]))],
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
) -> MetaSchemaVersionWriteResponse:
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
                "message": f"Schema {schema_path!r} not found",
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
                "message": f"Version {version!r} not found for schema {canonical_path!r}",
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
        created_by=git_author(user),
    )
    audit_resource_change(user.user_id, "meta_schema", canonical_path, "updated")
    return meta_schema_version_write_response(saved, path=canonical_path)


@router.delete(
    "/definitions/{schema_path:path}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_action(scopes_dict["schema_delete"]))],
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
                "message": f"Schema {schema_path!r} not found",
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
    dependencies=[Depends(require_action(scopes_dict["schema_read"]))],
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


@router.get(
    "/{source_name}/columns",
    response_model=PaginatedResponse[SchemaColumn],
)
async def get_schema_columns(
    source_name: str,
    request: Request,
    user: CurrentUser,
    registry: SourceReg,
    version: str | None = Query(
        None,
        description="Source version id (defaults to deployed_version)",
    ),
    pagination: PaginationParams = Depends(),
    _auth: None = Depends(require_action("source:read")),
) -> PaginatedResponse[SchemaColumn]:
    """Get composed schema columns for a source version (profile + meta/derived/additional).

    Use ``per_page=-1`` to return all columns in one page.
    """
    from dfe_engine.schema.schema_builder_v2 import SchemaBuildError, SchemaBuilderV2
    from dfe_engine.source.type_registry import TypeRegistry

    try:
        source = registry.get_source(source_name)
    except SourceNotFoundError:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Source {source_name!r} not found"},
        )

    version_id = version or source.runtime_version_id()
    if version_id not in source.versions:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "not_found",
                "message": f"Version '{version_id}' not found for source '{source_name}'",
            },
        )

    snap = source.versions[version_id]
    if not SchemaBuilderV2.version_snapshot_has_schema_files(snap):
        raise HTTPException(
            status_code=404,
            detail={
                "code": "no_schema",
                "message": f"Source {source_name!r} version '{version_id}' has no schema configured",
            },
        )

    settings = get_settings()
    builder = SchemaBuilderV2(
        TypeRegistry.default(),
        schemas_base_dir=settings.schemas.schemas_dir or None,
    )
    try:
        columns = builder.load_columns_for_source_version(source, source_version=version_id)
    except SchemaBuildError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "schema_error", "message": str(exc)},
        ) from exc

    all_columns = [
        SchemaColumn(
            name=col.name,
            type=col.type,
            use_case=getattr(col, "use_case", "") or "",
            attribute=(
                ", ".join(col.attribute)
                if isinstance(col.attribute, list)
                else (getattr(col, "attribute", "") or "")
            ),
            description=getattr(col, "comment", None) or getattr(col, "description", "") or "",
        )
        for col in columns
    ]
    return PaginatedResponse.from_list(
        all_columns,
        pagination.page,
        pagination.per_page,
    )


@router.post("/{source_name}/build", response_model=SchemaBuildResult)
async def build_schema(
    source_name: str,
    request: Request,
    user: CurrentUser,
    registry: SourceReg,
    version: str | None = Query(
        None,
        description="Source version id (defaults to deployed_version)",
    ),
    _auth: None = Depends(require_action("config:write")),
) -> SchemaBuildResult:
    """Build complete schema (DDL) from a source version snapshot.

    Runs the v2 YAML → DDL pipeline and returns the generated DDL
    without executing it against ClickHouse.
    """
    from dfe_engine.schema.schema_builder_v2 import SchemaBuildError, SchemaBuilderV2
    from dfe_engine.source.type_registry import TypeRegistry

    try:
        source = registry.get_source(source_name)
    except SourceNotFoundError:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Source {source_name!r} not found"},
        )

    version_id = version or source.runtime_version_id()
    if version_id not in source.versions:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "not_found",
                "message": f"Version '{version_id}' not found for source '{source_name}'",
            },
        )

    snap = source.versions[version_id]
    if not SchemaBuilderV2.version_snapshot_has_schema_files(snap):
        raise HTTPException(
            status_code=404,
            detail={
                "code": "no_schema",
                "message": f"Source '{source_name}' version '{version_id}' has no schema configured",
            },
        )

    settings = get_settings()
    builder = SchemaBuilderV2(
        TypeRegistry.default(),
        schemas_base_dir=settings.schemas.schemas_dir or None,
    )
    try:
        result = builder.build_for_source_version(source, source_version=version_id)
    except SchemaBuildError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "build_error", "message": str(exc)},
        ) from exc

    columns = [
        SchemaColumn(
            name=col.name,
            type=col.type,
            use_case=getattr(col, "use_case", "") or "",
            attribute=(
                ", ".join(col.attribute)
                if isinstance(col.attribute, list)
                else (getattr(col, "attribute", "") or "")
            ),
            description=getattr(col, "comment", None) or getattr(col, "description", "") or "",
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
        version=version_id,
        columns=columns,
        ddl=ddl,
    )


# ── JSON field promotion ────────────────────────────────────


def _resolve_meta_schema(rel, source_name, schema_registry):
    """Resolve a meta-schema reference: (canonical path, MetaSchema, current columns).

    Raises:
        HTTPException: 404 when the referenced meta-schema is missing.
    """
    from dfe_engine.schema.registry import SchemaNotFoundError, canonical_schema_path

    canonical = canonical_schema_path(rel.removesuffix(".yaml"))
    try:
        meta = schema_registry.get_schema(canonical)
    except SchemaNotFoundError:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "not_found",
                "message": f"Meta-schema '{canonical}' not found for source '{source_name}'",
            },
        ) from None
    return canonical, meta, meta.versions[meta.current].columns


def _resolve_source_meta_schema(source, schema_registry):
    """Resolve a source's (deployed) meta-schema: (canonical path, MetaSchema, columns).

    Raises:
        HTTPException: 422 when the source has no meta_schema, 404 when the
        referenced meta-schema is missing.
    """
    rel = source.schema_config.meta_schema
    if not rel:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "no_meta_schema",
                "message": f"Source '{source.source}' has no meta_schema configured",
            },
        )
    return _resolve_meta_schema(rel, source.source, schema_registry)


def _discovery_target(source, ver, schema_registry):
    """Resolve where to discover a source version's ``_json`` paths.

    A version with a ``meta_schema`` owns its own table, so discovery targets
    ``db.<source>`` with that schema's columns. Otherwise the version's data
    still lives in the shared catch-all landing table, so discovery targets
    ``db.<landing>`` filtered by the version's match rule.

    Returns:
        ``(target_table, match_field, match_value, existing_columns)``.
    """
    ver_schema = ver.effective_schema()
    if ver_schema.meta_schema:
        _canonical, _meta, columns = _resolve_meta_schema(
            ver_schema.meta_schema, source.source, schema_registry
        )
        return source.table_name, None, None, columns
    return get_settings().clickhouse.landing_table, ver.match.field, ver.match.value, []


def _resolve_source_version(source, version, source_name):
    """Resolve a source version snapshot (defaults to current).

    Raises:
        HTTPException: 404 when the requested version is not defined.
    """
    version_id = version or source.current
    try:
        return version_id, source.version(version_id)
    except ValueError:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "not_found",
                "message": f"Version '{version_id}' not found for source '{source_name}'",
            },
        ) from None


@router.get(
    "/{source_name}/json-paths",
    response_model=JsonPathsResponse,
    dependencies=[Depends(require_action("schema:read"))],
)
async def discover_json_paths(
    source_name: str,
    user: CurrentUser,
    source_registry: SourceReg,
    schema_registry: SchemaReg,
    ch: ClickHouseClient,
    samples: int | None = Query(
        None, ge=1, le=100, description="Random distinct example values per path"
    ),
    stats: bool = Query(
        False, description="Include coverage_pct + distinct_count (expensive, opt-in)"
    ),
    paths: str | None = Query(
        None, description="Comma-separated paths to restrict discovery + sampling"
    ),
    version: str | None = Query(
        None, description="Source version to discover against (defaults to current)"
    ),
) -> JsonPathsResponse:
    """Discover JSON paths inside a source's ``_json`` column.

    Resolves the requested ``version`` (or the source's current version). If that
    version defines a ``meta_schema`` the source owns its own table and discovery
    runs against ``db.<source>``. Otherwise the source's data still lives in the
    shared catch-all landing table, so discovery runs against ``db.<landing>``
    filtered by the version's match rule.

    Returns one record per path with observed types, a suggested column name,
    and whether the path is already promoted. ``?samples=N`` adds random
    distinct example values; ``?stats=true`` adds coverage + distinct counts.
    """
    from dfe_engine.services.schema.json_promotion_service import (
        JSON_COLUMN,
        JsonPromotionError,
        discover_paths,
    )

    try:
        source = source_registry.get_source(source_name)
    except SourceNotFoundError:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Source '{source_name}' not found"},
        ) from None

    _version_id, ver = _resolve_source_version(source, version, source_name)
    target_table, match_field, match_value, columns = _discovery_target(
        source, ver, schema_registry
    )

    db = get_settings().clickhouse.effective_data_database
    path_filter = [p.strip() for p in paths.split(",") if p.strip()] if paths else None

    try:
        discovered = discover_paths(
            ch,
            db=db,
            source=target_table,
            existing_columns=columns,
            match_field=match_field,
            match_value=match_value,
            paths=path_filter,
            samples=samples,
            stats=stats,
        )
    except JsonPromotionError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "discovery_failed", "message": str(exc)},
        ) from exc

    return JsonPathsResponse(
        source_name=source_name,
        table=f"{db}.{target_table}",
        json_column=JSON_COLUMN,
        paths=[
            JsonPathInfo(
                path=d.path,
                types=d.types,
                is_consistent=d.is_consistent,
                promoted_to=d.promoted_to,
                column=DraftColumn(
                    name=d.suggested_column_name,
                    type=d.column_type or "json",
                    attribute=d.column_attributes,
                    expr=d.copy_expr,
                    comment=f"Promoted from _json.{d.path}",
                ),
                coverage_pct=d.coverage_pct,
                distinct_count=d.distinct_count,
                samples=d.samples,
            )
            for d in discovered
        ],
    )


@router.get(
    "/{source_name}/sample-rows",
    response_model=SampleRowsResponse,
    dependencies=[Depends(require_action("schema:read"))],
)
async def sample_source_rows(
    source_name: str,
    user: CurrentUser,
    source_registry: SourceReg,
    schema_registry: SchemaReg,
    ch: ClickHouseClient,
    limit: int = Query(10, ge=1, le=100, description="Number of random rows to sample"),
    version: str | None = Query(
        None, description="Source version to sample against (defaults to current)"
    ),
) -> SampleRowsResponse:
    """Return random sample rows for a source, scoped to its match rule.

    Resolves the requested ``version`` (or the source's current version). A
    version with a ``meta_schema`` owns its own table, so sampling runs against
    ``db.<source>`` unfiltered. Otherwise the version's data still lives in the
    shared catch-all landing table, so sampling runs against ``db.<landing>``
    filtered by the version's match rule.

    Intended for inspecting real data while authoring a match condition or CEL
    before promoting any JSON path -- a row-level companion to the per-path
    ``?samples=N`` on ``/json-paths``.
    """
    from dfe_engine.services.schema.json_promotion_service import (
        JsonPromotionError,
        sample_rows,
    )

    try:
        source = source_registry.get_source(source_name)
    except SourceNotFoundError:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Source '{source_name}' not found"},
        ) from None

    _version_id, ver = _resolve_source_version(source, version, source_name)
    target_table, match_field, match_value, _columns = _discovery_target(
        source, ver, schema_registry
    )

    db = get_settings().clickhouse.effective_data_database

    try:
        columns, rows = sample_rows(
            ch,
            db=db,
            source=target_table,
            match_field=match_field,
            match_value=match_value,
            limit=limit,
        )
    except JsonPromotionError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "sample_failed", "message": str(exc)},
        ) from exc

    return SampleRowsResponse(
        source_name=source_name,
        table=f"{db}.{target_table}",
        match_field=match_field,
        match_value=match_value,
        columns=columns,
        rows=rows,
    )


@router.post(
    "/{source_name}/promote-field",
    response_model=PromoteFieldResponse,
    dependencies=[Depends(require_action("schema:write"))],
)
async def promote_field(
    source_name: str,
    body: PromoteFieldRequest,
    user: CurrentUser,
    source_registry: SourceReg,
    schema_registry: SchemaReg,
    ch: ClickHouseClient,
    dry_run: bool = Query(False, description="Return the proposed diff without committing"),
) -> PromoteFieldResponse:
    """Promote JSON path(s) into dedicated typed columns.

    Creates a new schema version on the source's meta-schema, adding one column
    per path with a ``@copy`` directive so dfe-loader copies the value forward.
    ``?dry_run=true`` returns the proposed diff + DDL without committing.
    """
    from dfe_engine.schema.schema_manager import (
        SchemaManager,
        SchemaVersionError,
        next_version_for_type,
    )
    from dfe_engine.services.schema.json_promotion_service import (
        JsonPromotionError,
        PromotionRequest,
        build_promotion_columns,
        discover_paths,
        promotion_preview_ddl,
    )
    from dfe_engine.source.type_registry import TypeRegistry

    try:
        source = source_registry.get_source(source_name)
    except SourceNotFoundError:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Source '{source_name}' not found"},
        ) from None

    canonical, meta, columns = _resolve_source_meta_schema(source, schema_registry)

    is_batch = isinstance(body.json_path, list)
    raw_paths = body.json_path if is_batch else [body.json_path]
    requests = [
        PromotionRequest(
            json_path=p,
            column_name=None if is_batch else body.column_name,
            data_type=body.data_type,
            index_type=body.index_type,
        )
        for p in raw_paths
    ]

    db = get_settings().clickhouse.effective_data_database

    # Discover ClickHouse types only when a request relies on auto-derivation.
    path_types: dict[str, list[str]] = {}
    if any(r.data_type is None for r in requests):
        try:
            discovered = discover_paths(
                ch,
                db=db,
                source=source.table_name,
                existing_columns=columns,
                paths=[r.json_path for r in requests],
            )
        except JsonPromotionError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"code": "discovery_failed", "message": str(exc)},
            ) from exc
        path_types = {d.path: d.types for d in discovered}

    outcomes = build_promotion_columns(
        columns,
        requests,
        type_registry=TypeRegistry.default(),
        path_types=path_types,
    )
    results = [
        PromoteResult(
            json_path=o.json_path,
            status=o.status,
            column_name=o.column_name,
            data_type=o.data_type,
            index_type=o.index_type,
            copy_cel=o.copy_cel,
            error=o.error,
        )
        for o in outcomes
    ]
    new_columns = [o.column for o in outcomes if o.status == "ok" and o.column is not None]
    has_error = any(o.status == "error" for o in outcomes)

    if dry_run:
        diff = SchemaDiff(
            new_columns=[
                SchemaColumn(
                    name=c.name,
                    type=c.type,
                    use_case=c.use_case or "",
                    attribute=", ".join(c.attribute),
                    description=c.comment or "",
                )
                for c in new_columns
            ],
            ddl=promotion_preview_ddl(source.table_name, new_columns, db=db),
            copy_directives=[c.expr for c in new_columns if c.expr],
        )
        return PromoteFieldResponse(
            source_name=source_name, schema_version=None, results=results, diff=diff
        )

    if body.atomic and has_error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "promotion_failed",
                "message": "Atomic promotion aborted: one or more paths failed",
                "results": [r.model_dump() for r in results],
            },
        )

    if not new_columns:
        return PromoteFieldResponse(source_name=source_name, schema_version=None, results=results)

    merged = [col.to_yaml_dict() for col in columns]
    merged.extend(
        MetaSchemaColumn(
            name=c.name,
            type=c.type,
            attribute=(c.attribute or None),
            use_case=c.use_case,
            expr=c.expr,
            comment=c.comment,
        ).to_yaml_dict()
        for c in new_columns
    )

    new_ver = next_version_for_type(meta.current, "addition")
    yaml_path = schema_registry._yaml_path(canonical)
    try:
        if schema_registry.schema_version_exists(canonical, new_ver):
            raise SchemaVersionError(f"Version '{new_ver}' already exists")
        SchemaManager.add_version(
            yaml_path,
            new_ver,
            merged,
            type="addition",
            summary=f"Promote {len(new_columns)} JSON field(s) to columns",
            set_current=True,
        )
    except SchemaVersionError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "validation_error", "message": str(exc)},
        ) from exc

    description = f"schema: {canonical} (promote {len(new_columns)} JSON field(s))"
    schema_registry.notify_schema_file_updated(
        canonical, description=description, created_by=git_author(user)
    )
    audit_resource_change(user.user_id, "meta_schema", canonical, "updated")

    return PromoteFieldResponse(source_name=source_name, schema_version=new_ver, results=results)
