"""Sources router — CRUD, pagination, search, sort, bulk operations.

GET    /api/v1/sources                  → Paginated source list
POST   /api/v1/sources                  → Create source
GET    /api/v1/sources/{name}           → Get source details
GET    /api/v1/sources/{name}/versions  → Get one version snapshot
GET    /api/v1/sources/{name}/columns   → Composed schema columns for a version
POST   /api/v1/sources/{name}/build     → Build DDL from a version snapshot
POST   /api/v1/sources/{name}/plan      → Dry-run deploy plan (not persisted)
POST   /api/v1/sources/{name}/deploy    → Deploy version to ClickHouse
PUT    /api/v1/sources/{name}           → Update source
DELETE /api/v1/sources/{name}           → Delete source
POST   /api/v1/sources/bulk             → Bulk enable/disable/delete
POST   /api/v1/sources/seed             → Seed built-in defaults
"""

from __future__ import annotations

from typing import Any, NoReturn

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from dfe_engine.api.deps import ClickHouseClient, CurrentUser, SourceReg, require_action
from dfe_engine.api.errors import MatchConflictErrorResponse, SourceCreateConflictResponse
from dfe_engine.api.pagination import PaginatedResponse, PaginationParams, apply_search, apply_sort
from dfe_engine.auth.audit import audit_resource_change
from dfe_engine.auth.rbac_scopes import scopes_dict
from dfe_engine.git_identity import git_author
from dfe_engine.settings import get_settings
from dfe_engine.source.deployment import (
    SourceBuildArtifact,
    SourceDeployArtifact,
    SourceDeploymentStore,
    SourcePlanArtifact,
    deploy_statements_for_build,
    ensure_build_artifact,
    execute_ddl_statements,
    plan_from_build,
    plan_ready_status,
)
from dfe_engine.source.models import (
    PaginatedSourceSummaryResponse,
    Source,
    SourceSummaryObject,
    SourceVersion,
    SourceVersionGetResponse,
    SourceWriteRequest,
)
from dfe_engine.source.registry import (
    SourceMatchConflictError,
    SourceNotFoundError,
    SourceValidationError,
)

router = APIRouter(prefix="/sources", tags=["Sources"])


def _raise_save_validation_http(exc: SourceValidationError) -> NoReturn:
    """Map registry validation errors to HTTP responses (never 500)."""
    if isinstance(exc, SourceMatchConflictError):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "match_conflict",
                "message": str(exc),
                "source": exc.source,
                "conflicting_source": exc.conflicting_source,
                "field": exc.field,
                "value": exc.value,
            },
        ) from exc
    raise HTTPException(
        status_code=422,
        detail={
            "code": "validation_error",
            "message": str(exc),
        },
    ) from exc


# ── Response models ──────────────────────────────────────────


class SourceResponse(BaseModel):
    """Legacy compact metadata after create/update (prefer full ``Source`` on write)."""

    source: str
    message: str = "ok"
    current: str = Field(..., description="Working version id after the operation")
    deployed_version: str | None = Field(
        default=None,
        description="Version deployed to runtime (null until first deploy)",
    )
    versions: list[str] = Field(..., description="All version ids on the source")


def _source_response(source: Source, *, message: str) -> SourceResponse:
    return SourceResponse(
        source=source.source,
        message=message,
        current=source.current,
        deployed_version=source.deployed_version,
        versions=sorted(source.versions.keys()),
    )


class BulkActionRequest(BaseModel):
    """Bulk operation on multiple sources."""

    action: str = Field(description="Action: enable, disable, delete")
    sources: list[str] = Field(description="Source names to act on")


class BulkActionResponse(BaseModel):
    """Result of a bulk operation."""

    action: str
    succeeded: list[str] = Field(default_factory=list)
    failed: list[dict[str, str]] = Field(default_factory=list)


class SeedResponse(BaseModel):
    """Result of seeding built-in sources."""

    seeded: int = Field(description="Number of sources seeded")


class SchemaColumn(BaseModel):
    """A column in a composed source schema (API response)."""

    name: str
    type: str
    use_case: str = ""
    attribute: str = ""
    description: str = ""


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
    validation_errors: list[str] = Field(default_factory=list)
    built_at: str | None = Field(
        default=None,
        description="When the build was persisted (source-builds); omitted on live-only builds",
    )
    column_count: int | None = Field(
        default=None,
        description="Column count at build time when columns are not included in the payload",
    )


class SourcePlanResponse(BaseModel):
    """Dry-run ClickHouse deploy plan for a source version."""

    source_name: str
    version: str
    planned_at: str
    table_exists: bool = False
    validation_errors: list[str] = Field(default_factory=list)
    statements: list[str] = Field(default_factory=list)
    ddl: DDLResult | None = None
    ready: bool = False
    ready_reason: str | None = Field(
        default=None,
        description="Why the plan is or is not ready to deploy",
    )


class SourceDeployResponse(BaseModel):
    """Result of deploying a source version to ClickHouse."""

    source_name: str
    version: str
    success: bool
    deployed_version: str | None = None
    deployed_at: str
    ddl_executed: list[str] = Field(default_factory=list)
    ddl_failed: list[dict[str, str]] = Field(default_factory=list)


class SourceVersionDetail(SourceVersion):
    """Source version snapshot plus persisted build/deploy payloads."""

    source_build: SchemaBuildResult | None = Field(
        default=None,
        description="Last schema build for this version (source-builds)",
    )
    source_deployment: SourceDeployResponse | None = Field(
        default=None,
        description="Last deploy run for this version (source-deploys)",
    )


class SourceDetailResponse(Source):
    """Full source definition with per-version build/plan/deploy status."""

    versions: dict[str, SourceVersionDetail] = Field(
        default_factory=dict,
        description="Version id → configuration snapshot and pipeline artifacts",
    )


class SourceVersionGetDetailResponse(SourceVersionGetResponse):
    """Single-version GET with build/plan/deploy status on ``version``."""

    version: SourceVersionDetail = Field(
        ...,
        description="Configuration snapshot for ``selected`` plus pipeline artifacts",
    )


# ── Endpoints ────────────────────────────────────────────────


@router.get(
    "",
    response_model=PaginatedSourceSummaryResponse,
    dependencies=[Depends(require_action(scopes_dict["source_read"]))],
)
async def list_sources(
    user: CurrentUser,
    registry: SourceReg,
    pagination: PaginationParams = Depends(),
    search: str | None = Query(None, description="Search in source name and description"),
    enabled: bool | None = Query(None, description="Filter by enabled status"),
    sort_by: str | None = Query(
        None,
        description="Sort field (source, display_name, enabled, current, deployed_version, updated_at)",
    ),
    sort_order: str = Query("asc", description="Sort order: asc/desc"),
):
    """List sources with pagination, search, filtering, and a full object tree."""
    raw_sources = registry.list_sources(enabled_only=bool(enabled))

    if enabled is False:
        raw_sources = [s for s in raw_sources if not s.get("enabled", True)]

    raw_sources = apply_search(raw_sources, search, ["source", "display_name", "description"])
    raw_sources = apply_sort(raw_sources, sort_by, sort_order)

    summaries = [_to_summary(row) for row in raw_sources]
    return PaginatedSourceSummaryResponse.from_summaries(
        summaries, pagination.page, pagination.per_page
    )


@router.post(
    "",
    response_model=SourceResponse,
    status_code=201,
    responses={
        409: {
            "model": SourceCreateConflictResponse,
            "description": (
                "Source name already exists (code conflict), or receiver match duplicates "
                "another enabled source (code match_conflict)"
            ),
        },
    },
    dependencies=[Depends(require_action(scopes_dict["source_write"]))],
)
async def create_source(
    body: SourceWriteRequest,
    user: CurrentUser,
    registry: SourceReg,
):
    """Create a new source from a flat source definition (initial version ``1.0.0``).

    ``header`` is optional: when omitted, no common-header profile is stored on the
    version (DDL compose uses meta/derived columns only until a header is set).
    """
    name = body.source

    if not name:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "validation_error",
                "message": "'source' field is required",
            },
        )

    if registry.source_exists(name):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "conflict",
                "message": f"Source {name!r} already exists",
            },
        )

    try:
        source = registry.create_source_from_write(body, created_by=git_author(user))
    except SourceValidationError as e:
        _raise_save_validation_http(e)
    audit_resource_change(user.user_id, "source", source.source, "created")
    return _source_response(source, message="created")


@router.get(
    "/{name}/versions",
    response_model=SourceVersionGetDetailResponse,
    dependencies=[Depends(require_action(scopes_dict["source_read"]))],
)
async def get_source_version(
    name: str,
    user: CurrentUser,
    registry: SourceReg,
    version: str = Query(
        ...,
        min_length=1,
        description="Source version id to return (required)",
    ),
):
    """Get one immutable source version snapshot by id, with build/deploy status."""
    try:
        source = registry.get_source(name)
    except SourceNotFoundError:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "not_found",
                "message": f"Source '{name}' not found",
            },
        ) from None

    if version not in source.versions:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "not_found",
                "message": f"Version '{version}' not found for source '{name}'",
            },
        )

    store = SourceDeploymentStore.from_settings(get_settings())
    version_ids = sorted(source.versions.keys())
    snap = source.versions[version]
    return SourceVersionGetDetailResponse(
        source=source.source,
        display_name=source.display_name,
        description=source.description,
        enabled=source.enabled,
        current=source.current,
        deployed_version=source.deployed_version,
        selected=version,
        versions=version_ids,
        version=_version_detail_from_snapshot(source.source, version, snap, store),
    )


@router.get(
    "/{name}/columns",
    response_model=PaginatedResponse[SchemaColumn],
    dependencies=[Depends(require_action(scopes_dict["source_read"]))],
)
async def get_source_schema_columns(
    name: str,
    user: CurrentUser,
    registry: SourceReg,
    version: str | None = Query(
        None,
        description="Source version id (defaults to deployed_version)",
    ),
    pagination: PaginationParams = Depends(),
) -> PaginatedResponse[SchemaColumn]:
    """Get composed schema columns for a source version (profile + meta/derived/additional).

    Use ``per_page=-1`` to return all columns in one page.
    """
    from dfe_engine.schema.schema_builder_v2 import SchemaBuildError, SchemaBuilderV2
    from dfe_engine.source.type_registry import TypeRegistry

    try:
        source = registry.get_source(name)
    except SourceNotFoundError:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Source '{name}' not found"},
        ) from None

    version_id = version or source.runtime_version_id()
    if version_id not in source.versions:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "not_found",
                "message": f"Version '{version_id}' not found for source '{name}'",
            },
        )

    snap = source.versions[version_id]
    if not SchemaBuilderV2.version_snapshot_has_schema_files(snap):
        raise HTTPException(
            status_code=404,
            detail={
                "code": "no_schema",
                "message": f"Source '{name}' version '{version_id}' has no schema configured",
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

    all_columns = [_to_api_schema_column(col) for col in columns]
    return PaginatedResponse.from_list(
        all_columns,
        pagination.page,
        pagination.per_page,
    )


@router.post(
    "/{name}/build",
    response_model=SchemaBuildResult,
    dependencies=[Depends(require_action(scopes_dict["source_deploy"]))],
)
async def build_source_schema(
    name: str,
    user: CurrentUser,
    registry: SourceReg,
    version: str | None = Query(
        None,
        description="Source version id (defaults to deployed_version)",
    ),
) -> SchemaBuildResult:
    """Build complete schema (DDL) from a source version snapshot.

    Runs the v2 YAML → DDL pipeline and returns the generated DDL
    without executing it against ClickHouse.
    """
    from dfe_engine.schema.schema_builder_v2 import SchemaBuildError, SchemaBuilderV2

    try:
        source = registry.get_source(name)
    except SourceNotFoundError:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Source '{name}' not found"},
        ) from None

    version_id = version or source.runtime_version_id()
    if version_id not in source.versions:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "not_found",
                "message": f"Version '{version_id}' not found for source '{name}'",
            },
        )

    snap = source.versions[version_id]
    if not SchemaBuilderV2.version_snapshot_has_schema_files(snap):
        raise HTTPException(
            status_code=404,
            detail={
                "code": "no_schema",
                "message": f"Source '{name}' version '{version_id}' has no schema configured",
            },
        )

    settings = get_settings()
    store = SourceDeploymentStore.from_settings(settings)
    try:
        result, _build_artifact = ensure_build_artifact(
            store,
            source,
            version_id=version_id,
            schemas_base_dir=settings.schemas.schemas_dir or None,
            refresh=True,
        )
    except SchemaBuildError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "build_error", "message": str(exc)},
        ) from exc

    columns = [_to_api_schema_column(col) for col in result.columns]

    ddl = None
    if result.create_table_ddl:
        ddl = DDLResult(
            source_name=name,
            create_table=result.create_table_ddl,
            views={k: v for k, v in (result.view_ddls or {}).items()},
        )

    audit_resource_change(user.user_id, "schema", name, "executed")
    return SchemaBuildResult(
        source_name=name,
        version=version_id,
        columns=columns,
        ddl=ddl,
        validation_errors=list(result.validation_errors),
    )


@router.post(
    "/{name}/plan",
    response_model=SourcePlanResponse,
    dependencies=[Depends(require_action(scopes_dict["source_deploy"]))],
)
async def plan_source_deploy(
    name: str,
    user: CurrentUser,
    registry: SourceReg,
    ch_client: ClickHouseClient,
    version: str | None = Query(
        None,
        description="Source version id (defaults to current working version)",
    ),
) -> SourcePlanResponse:
    """Dry-run ClickHouse deploy: DDL statements and validation errors (not persisted)."""
    from dfe_engine.schema.schema_builder_v2 import SchemaBuildError, SchemaBuilderV2
    from dfe_engine.source.type_registry import TypeRegistry

    source, version_id = _resolve_source_version(registry, name, version, default_current=True)
    snap = source.versions[version_id]
    if not SchemaBuilderV2.version_snapshot_has_schema_files(snap):
        raise HTTPException(
            status_code=404,
            detail={
                "code": "no_schema",
                "message": f"Source '{name}' version '{version_id}' has no schema configured",
            },
        )

    settings = get_settings()
    store = SourceDeploymentStore.from_settings(settings)
    try:
        result, _artifact = ensure_build_artifact(
            store,
            source,
            version_id=version_id,
            schemas_base_dir=settings.schemas.schemas_dir or None,
            refresh=True,
        )
    except SchemaBuildError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "build_error", "message": str(exc)},
        ) from exc

    builder = SchemaBuilderV2(
        TypeRegistry.default(),
        schemas_base_dir=settings.schemas.schemas_dir or None,
    )
    db = settings.clickhouse.effective_data_database
    statements, table_exists = deploy_statements_for_build(
        builder,
        source,
        version_id,
        result,
        db=db,
        ch_client=ch_client,
    )
    plan = plan_from_build(
        result,
        version=version_id,
        statements=statements,
        table_exists=table_exists,
    )
    audit_resource_change(user.user_id, "source", name, "planned")
    return _plan_to_response(plan)


@router.post(
    "/{name}/deploy",
    response_model=SourceDeployResponse,
    dependencies=[Depends(require_action(scopes_dict["source_deploy"]))],
)
async def deploy_source(
    name: str,
    user: CurrentUser,
    registry: SourceReg,
    ch_client: ClickHouseClient,
    version: str | None = Query(
        None,
        description="Source version id to deploy (defaults to current working version)",
    ),
) -> SourceDeployResponse:
    """Apply DDL for a source version to ClickHouse and set deployed_version."""
    from dfe_engine.schema.schema_builder_v2 import SchemaBuildError, SchemaBuilderV2
    from dfe_engine.source.type_registry import TypeRegistry

    source, version_id = _resolve_source_version(registry, name, version, default_current=True)
    snap = source.versions[version_id]
    if not SchemaBuilderV2.version_snapshot_has_schema_files(snap):
        raise HTTPException(
            status_code=404,
            detail={
                "code": "no_schema",
                "message": f"Source '{name}' version '{version_id}' has no schema configured",
            },
        )

    settings = get_settings()
    store = SourceDeploymentStore.from_settings(settings)
    try:
        result, _artifact = ensure_build_artifact(
            store,
            source,
            version_id=version_id,
            schemas_base_dir=settings.schemas.schemas_dir or None,
            refresh=True,
        )
    except SchemaBuildError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "build_error", "message": str(exc)},
        ) from exc
    builder = SchemaBuilderV2(
        TypeRegistry.default(),
        schemas_base_dir=settings.schemas.schemas_dir or None,
    )
    statements, table_exists = deploy_statements_for_build(
        builder,
        source,
        version_id,
        result,
        db=settings.clickhouse.effective_data_database,
        ch_client=ch_client,
    )
    ready, ready_reason = plan_ready_status(
        validation_errors=list(result.validation_errors),
        statements=statements,
        table_exists=table_exists,
    )
    if not ready:
        code = "plan_not_ready" if result.validation_errors else "nothing_to_deploy"
        raise HTTPException(
            status_code=400,
            detail={
                "code": code,
                "message": ready_reason,
                "validation_errors": list(result.validation_errors),
            },
        )

    executed, failed = execute_ddl_statements(ch_client, statements)
    deployed_at = _utc_now_iso()
    success = not failed
    deployed_version: str | None = None

    if success:
        try:
            updated = registry.set_deployed_version(
                name,
                version_id,
                created_by=git_author(user),
            )
            deployed_version = updated.deployed_version
        except SourceValidationError as exc:
            success = False
            failed.append(("", str(exc)))

    deploy_artifact = SourceDeployArtifact(
        source_name=name,
        version=version_id,
        deployed_at=deployed_at,
        success=success,
        deployed_version=deployed_version,
        ddl_executed=executed,
        ddl_failed=[{"statement": stmt, "error": err} for stmt, err in failed],
    )
    store.save_deploy(deploy_artifact, source)

    if success:
        audit_resource_change(user.user_id, "source", name, "deployed")
    else:
        audit_resource_change(user.user_id, "source", name, "deploy_failed")

    return SourceDeployResponse(
        source_name=name,
        version=version_id,
        success=success,
        deployed_version=deployed_version,
        deployed_at=deployed_at,
        ddl_executed=executed,
        ddl_failed=deploy_artifact.ddl_failed,
    )


@router.get(
    "/{name}",
    response_model=SourceDetailResponse,
    dependencies=[Depends(require_action(scopes_dict["source_read"]))],
)
async def get_source(name: str, user: CurrentUser, registry: SourceReg):
    """Get a full source definition by name, including build/deploy per version."""
    try:
        source = registry.get_source(name)
    except SourceNotFoundError:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "not_found",
                "message": f"Source {name!r} not found",
            },
        ) from None
    store = SourceDeploymentStore.from_settings(get_settings())
    return _to_source_detail_response(source, store)


@router.put(
    "/{name}",
    response_model=SourceResponse,
    responses={
        409: {
            "model": MatchConflictErrorResponse,
            "description": "Receiver match duplicates another enabled source",
        },
    },
    dependencies=[Depends(require_action(scopes_dict["source_write"]))],
)
async def update_source(
    name: str,
    body: SourceWriteRequest,
    user: CurrentUser,
    registry: SourceReg,
):
    """Update a source from a flat revision body.

    Before the first deploy, edits update the working version in place. After deploy, a new
    major version is created only when ``current`` equals ``deployed_version`` and schema pins
    (``meta_schema``, ``meta_schema_version``, ``derived_schema``, ``additional_fields``),
    ``field_mappings``, ``sigma``, or ``transform`` change. Draft versions (``current`` not deployed) update
    in place.
    """
    from dfe_engine.source.registry import SourceNotFoundError

    if not registry.source_exists(name):
        raise HTTPException(
            status_code=404,
            detail={
                "code": "not_found",
                "message": f"Source {name!r} not found",
            },
        )

    try:
        source = registry.update_source_from_write(name, body, created_by=git_author(user))
    except SourceNotFoundError:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "not_found",
                "message": f"Source '{name}' not found",
            },
        ) from None
    except SourceValidationError as e:
        _raise_save_validation_http(e)
    audit_resource_change(user.user_id, "source", source.source, "updated")
    return _source_response(source, message="updated")


@router.delete(
    "/{name}",
    status_code=204,
    dependencies=[Depends(require_action(scopes_dict["source_delete"]))],
)
async def delete_source(name: str, user: CurrentUser, registry: SourceReg):
    """Delete a source by name."""
    if not registry.source_exists(name):
        raise HTTPException(
            status_code=404,
            detail={
                "code": "not_found",
                "message": f"Source {name!r} not found",
            },
        )
    registry.delete_source(name)
    audit_resource_change(user.user_id, "source", name, "deleted")


@router.post(
    "/bulk",
    response_model=BulkActionResponse,
    dependencies=[Depends(require_action(scopes_dict["source_write"]))],
)
async def bulk_action(
    body: BulkActionRequest,
    user: CurrentUser,
    registry: SourceReg,
):
    """Perform a bulk action (enable, disable, delete) on multiple sources."""
    if body.action not in ("enable", "disable", "delete"):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "validation_error",
                "message": f"Unknown action {body.action!r}. Must be: enable, disable, delete",
            },
        )

    succeeded: list[str] = []
    failed: list[dict[str, str]] = []

    for name in body.sources:
        try:
            if body.action == "delete":
                registry.delete_source(name)
            else:
                source = registry.get_source(name)
                source.enabled = body.action == "enable"
                registry.save_source(source, created_by=git_author(user))
            succeeded.append(name)
        except Exception as e:
            failed.append({"source": name, "error": str(e)})

    result = BulkActionResponse(action=body.action, succeeded=succeeded, failed=failed)
    if succeeded:
        audit_resource_change(user.user_id, "source", ",".join(succeeded), body.action)
    return result


@router.post(
    "/seed",
    response_model=SeedResponse,
    dependencies=[Depends(require_action(scopes_dict["source_write"]))],
)
async def seed_sources(user: CurrentUser, registry: SourceReg):
    """Seed built-in default source definitions. Non-destructive (skips existing)."""
    count = registry.seed_builtin_sources(overwrite=False)
    audit_resource_change(user.user_id, "source", "all", "seeded")
    return SeedResponse(seeded=count)


# ── Helpers ──────────────────────────────────────────────────


def _utc_now_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(tz=UTC).isoformat()


def _resolve_source_version(
    registry: SourceReg,
    name: str,
    version: str | None,
    *,
    default_current: bool = False,
) -> tuple[Source, str]:
    """Resolve source and version id for schema operations."""
    try:
        source = registry.get_source(name)
    except SourceNotFoundError:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Source '{name}' not found"},
        ) from None

    if version:
        version_id = version
    elif default_current:
        version_id = source.current
    else:
        version_id = source.runtime_version_id()

    if version_id not in source.versions:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "not_found",
                "message": f"Version '{version_id}' not found for source '{name}'",
            },
        )
    return source, version_id


def _version_detail_from_snapshot(
    source_name: str,
    version_id: str,
    snap: SourceVersion,
    store: SourceDeploymentStore,
) -> SourceVersionDetail:
    build = store.load_build(source_name, version_id)
    deploy = store.load_deploy(source_name, version_id)
    return SourceVersionDetail(
        **snap.model_dump(mode="json"),
        source_build=_build_to_response(build) if build else None,
        source_deployment=_deploy_to_response(deploy) if deploy else None,
    )


def _build_to_response(artifact: SourceBuildArtifact) -> SchemaBuildResult:
    views = dict(artifact.view_ddls)
    if artifact.sigma_view_ddl:
        views.setdefault("sigma", artifact.sigma_view_ddl)
    ddl = None
    if artifact.create_table_ddl:
        ddl = DDLResult(
            source_name=artifact.source_name,
            create_table=artifact.create_table_ddl,
            views=views,
        )
    return SchemaBuildResult(
        source_name=artifact.source_name,
        version=artifact.version,
        columns=[],
        ddl=ddl,
        validation_errors=list(artifact.validation_errors),
        built_at=artifact.built_at,
        column_count=artifact.column_count,
    )


def _deploy_to_response(artifact: SourceDeployArtifact) -> SourceDeployResponse:
    return SourceDeployResponse(
        source_name=artifact.source_name,
        version=artifact.version,
        success=artifact.success,
        deployed_version=artifact.deployed_version,
        deployed_at=artifact.deployed_at,
        ddl_executed=list(artifact.ddl_executed),
        ddl_failed=list(artifact.ddl_failed),
    )


def _to_source_detail_response(
    source: Source,
    store: SourceDeploymentStore,
) -> SourceDetailResponse:
    versions: dict[str, SourceVersionDetail] = {}
    for version_id, snap in source.versions.items():
        versions[version_id] = _version_detail_from_snapshot(source.source, version_id, snap, store)
    return SourceDetailResponse(
        **source.model_dump(mode="json", exclude={"versions"}),
        versions=versions,
    )


def _plan_to_response(plan: SourcePlanArtifact) -> SourcePlanResponse:
    ddl = None
    if plan.create_table_ddl:
        ddl = DDLResult(
            source_name=plan.source_name,
            create_table=plan.create_table_ddl,
            views=dict(plan.view_ddls),
        )
    return SourcePlanResponse(
        source_name=plan.source_name,
        version=plan.version,
        planned_at=plan.planned_at,
        table_exists=plan.table_exists,
        validation_errors=list(plan.validation_errors),
        statements=list(plan.statements),
        ddl=ddl,
        ready=plan.ready,
        ready_reason=plan.ready_reason,
    )


def _to_summary(raw: dict[str, Any]) -> SourceSummaryObject:
    """Convert registry list row to ``SourceSummaryObject``."""
    return SourceSummaryObject(
        name=raw.get("source", ""),
        display_name=raw.get("display_name"),
        description=raw.get("description"),
        enabled=raw.get("enabled", True),
        current=raw.get("current", "1.0.0"),
        deployed_version=raw.get("deployed_version"),
        versions=list(raw.get("versions") or []),
        updated_at=raw.get("updated_at") or "",
        header_type=raw.get("header_type"),
        has_transform=bool(raw.get("has_transform")),
        has_fetcher=bool(raw.get("has_fetcher")),
        mapping_standards=list(raw.get("mapping_standards") or []),
    )


def _to_api_schema_column(col: Any) -> SchemaColumn:
    return SchemaColumn(
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
