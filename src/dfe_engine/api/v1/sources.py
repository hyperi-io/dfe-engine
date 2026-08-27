"""Sources router — CRUD, pagination, search, sort, bulk operations.

GET    /api/v1/sources                  → Paginated source list
POST   /api/v1/sources                  → Create source
GET    /api/v1/sources/{name}           → Get source details
GET    /api/v1/sources/{name}/versions/{version}  → Get one version snapshot
GET    /api/v1/sources/{name}/columns   → Composed schema columns for a version
POST   /api/v1/sources/{name}/build     → Build DDL from a version snapshot
POST   /api/v1/sources/{name}/plan      → Dry-run deploy plan (not persisted)
POST   /api/v1/sources/{name}/deploy    → Deploy version to ClickHouse
PUT    /api/v1/sources/{name}           → Update source
PATCH  /api/v1/sources/{name}           → Enable or disable source
DELETE /api/v1/sources/{name}           → Delete source
POST   /api/v1/sources/bulk             → Bulk enable/disable/delete
POST   /api/v1/sources/seed             → Seed built-in defaults
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, NoReturn

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator
from scalo.logger import logger

from dfe_engine.api.deps import ClickHouseClient, CurrentUser, Settings, SourceReg, require_action
from dfe_engine.api.errors import MatchConflictErrorResponse, SourceCreateConflictResponse
from dfe_engine.api.pagination import PaginatedResponse, PaginationParams, apply_search, apply_sort
from dfe_engine.auth.audit import audit_resource_change
from dfe_engine.auth.rbac_scopes import scopes_dict
from dfe_engine.git_identity import git_author
from dfe_engine.settings import get_settings
from dfe_engine.source.deployment import (
    SchemaDeployResult,
    SourceBuildArtifact,
    SourceDeploymentStore,
    SourcePlanArtifact,
    deploy_statements_for_build,
    ensure_build_artifact,
    plan_from_build,
    previous_deployed_version_ids,
)
from dfe_engine.source.models import (
    PaginatedSourceSummaryResponse,
    Source,
    SourceState,
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


class SourceEnabledPatchRequest(BaseModel):
    """Partial update for source lifecycle state only.

    Send ``state`` for the tri-state (active | dormant | disabled), or the
    compat boolean ``enabled`` (True -> active, False -> disabled). ``state``
    wins when both are sent.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = Field(
        default=None,
        description="Compat boolean lifecycle (True -> active, False -> disabled)",
    )
    state: SourceState | None = Field(
        default=None,
        description="Tri-state lifecycle (active | dormant | disabled); wins over enabled",
    )

    @model_validator(mode="after")
    def _require_one(self) -> SourceEnabledPatchRequest:
        if self.state is None and self.enabled is None:
            raise ValueError("Send 'state' or 'enabled'")
        return self

    def target_state(self) -> SourceState:
        """The tri-state this patch requests."""
        if self.state is not None:
            return self.state
        return "active" if self.enabled else "disabled"


class BulkActionRequest(BaseModel):
    """Bulk operation on multiple sources."""

    action: str = Field(description="Action: enable, disable, dormant, delete")
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
    attribute: list[str] | None = None
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


class SourceVersionDetail(SourceVersion):
    """Source version snapshot plus persisted build/deploy payloads."""

    source_build: SchemaBuildResult | None = Field(
        default=None,
        description="Last schema build for this version (source-builds)",
    )
    source_deployment: SchemaDeployResult | None = Field(
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
    "/{name}/versions/{version}",
    response_model=SourceVersionGetDetailResponse,
    dependencies=[Depends(require_action(scopes_dict["source_read"]))],
)
async def get_source_version(
    name: str,
    version: str,
    user: CurrentUser,
    registry: SourceReg,
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
    deploy_doc = store.load_deploy_document(name)
    version_ids = sorted(source.versions.keys())
    prev_deployed = previous_deployed_version_ids(source, deploy_doc)
    snap = source.versions[version]
    return SourceVersionGetDetailResponse(
        source=source.source,
        display_name=source.display_name,
        description=source.description,
        state=source.state,
        enabled=source.enabled,
        current=source.current,
        deployed_version=source.deployed_version,
        selected=version,
        versions=version_ids,
        previous_deployed_versions=prev_deployed,
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
        default_engine=settings.clickhouse.default_engine,
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
    response_model=SchemaDeployResult,
    dependencies=[Depends(require_action(scopes_dict["source_write"]))],
)
async def deploy_source_schema(
    name: str,
    user: CurrentUser,
    registry: SourceReg,
    settings: Settings,
    version: str | None = Query(
        None, description="Source version id (defaults to deployed_version)"
    ),
    dry_run: bool = Query(
        False, description="Plan only: generate + validate the DDL without applying it"
    ),
) -> SchemaDeployResult:
    """Plan (``dry_run=true``) or deploy a source version's schema to ClickHouse.

    Runs the v2 YAML -> DDL pipeline. In plan mode the CREATE TABLE (+ any standard
    views) and validation errors are returned for review WITHOUT touching
    ClickHouse. In deploy mode the DDL is applied - it is idempotent (CREATE ... IF
    NOT EXISTS) so a re-deploy is a no-op. A schema that failed validation is never
    deployed.
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

    builder = SchemaBuilderV2(
        TypeRegistry.default(),
        schemas_base_dir=settings.schemas.schemas_dir or None,
        default_engine=settings.clickhouse.default_engine,
    )
    try:
        result = builder.build_for_source_version(source, source_version=version_id)
    except SchemaBuildError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "build_error", "message": str(exc)},
        ) from exc

    if not result.create_table_ddl:
        raise HTTPException(
            status_code=400,
            detail={"code": "no_ddl", "message": "no CREATE TABLE DDL was generated"},
        )

    views = dict(result.view_ddls or {})
    validation_errors = list(result.validation_errors or [])

    # Plan mode: hand the DDL + validation back for review; never touch ClickHouse.
    if dry_run:
        return SchemaDeployResult(
            source_name=name,
            version=version_id,
            dry_run=True,
            applied=False,
            create_table=result.create_table_ddl,
            views=views,
            validation_errors=validation_errors,
        )

    # Deploy mode: refuse to apply a schema that failed validation.
    if validation_errors:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "validation_failed",
                "message": "schema has validation errors; fix them or use dry_run to review",
                "errors": validation_errors,
            },
        )

    # Only reach for ClickHouse when actually deploying (keeps plan CH-free).
    from dfe_engine.clickhouse.clickhouse_manager import ClickHouseManager
    from dfe_engine.settings import get_clickhouse_config

    try:
        ch = ClickHouseManager.get_instance(get_clickhouse_config(settings)).get_clickhouse_client()
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "clickhouse_unavailable", "message": str(exc)},
        ) from exc

    db = settings.clickhouse.effective_data_database
    statements, _table_exists = deploy_statements_for_build(
        builder,
        source,
        version_id,
        result,
        db=db,
        ch_client=ch,
    )
    applied = 0
    try:
        ch.execute(f"CREATE DATABASE IF NOT EXISTS {db}")
        for stmt in statements:
            ch.execute(stmt)
            applied += 1
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail={
                "code": "ddl_apply_error",
                "message": f"ClickHouse rejected DDL after {applied} statement(s): {exc}",
            },
        ) from exc

    # The tier grant is database-wide, so the new table is readable the moment it
    # exists while its row policy would otherwise wait for the next reconcile.
    try:
        from dfe_engine.governance.ch.reconciler import fence_tables

        fence_tables(ch._client, database=db)
    except Exception as exc:
        logger.warning(f"Tenant fence not applied after deploying '{name}': {exc}")

    store = SourceDeploymentStore.from_settings(settings)
    deploy_result = SchemaDeployResult(
        source_name=name,
        version=version_id,
        dry_run=False,
        applied=True,
        create_table=result.create_table_ddl,
        views=views,
        validation_errors=[],
        statements_applied=applied,
    )
    store.save_deploy(deploy_result, source)
    try:
        registry.set_deployed_version(
            name,
            version_id,
            created_by=git_author(user),
            description=f"source: deploy {name} version {version_id}",
        )
    except SourceValidationError as exc:
        _raise_save_validation_http(exc)

    audit_resource_change(user.user_id, "schema", name, "deployed")
    return deploy_result


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
    ``views``, or ``transform`` change. Draft versions (``current`` not deployed) update
    in place.

    ``header`` is optional: when omitted, no header is stored on the written version snapshot
    (same as create). Send ``header`` explicitly to set or change it.
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


@router.patch(
    "/{name}",
    response_model=SourceResponse,
    responses={
        409: {
            "model": MatchConflictErrorResponse,
            "description": "Enabling would duplicate another enabled source's receiver match",
        },
    },
    dependencies=[Depends(require_action(scopes_dict["source_write"]))],
)
async def patch_source_enabled(
    name: str,
    body: SourceEnabledPatchRequest,
    user: CurrentUser,
    registry: SourceReg,
):
    """Set a source's lifecycle state without changing versioned configuration.

    Does not create a new source version. Activating may return ``409
    match_conflict`` if another non-disabled source already uses the same
    receiver match rule.
    """
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

    target = body.target_state()
    if source.state == target:
        return _source_response(source, message=target)

    updated = source.model_copy(update={"state": target})
    try:
        saved = registry.save_source(
            updated,
            created_by=git_author(user),
            description=f"source: set {name} {target}",
        )
    except SourceValidationError as e:
        _raise_save_validation_http(e)

    audit_resource_change(user.user_id, "source", saved.source, target)
    return _source_response(saved, message=target)


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
    registry.delete_source(name, created_by=git_author(user))
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
    """Perform a bulk action (enable, disable, dormant, delete) on multiple sources."""
    action_to_state = {"enable": "active", "disable": "disabled", "dormant": "dormant"}
    if body.action not in (*action_to_state, "delete"):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "validation_error",
                "message": (
                    f"Unknown action {body.action!r}. Must be: enable, disable, dormant, delete"
                ),
            },
        )

    succeeded: list[str] = []
    failed: list[dict[str, str]] = []

    for name in body.sources:
        try:
            if body.action == "delete":
                registry.delete_source(name, created_by=git_author(user))
            else:
                source = registry.get_source(name)
                updated = source.model_copy(update={"state": action_to_state[body.action]})
                registry.save_source(updated, created_by=git_author(user))
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
        source_deployment=deploy,
    )


def _build_to_response(artifact: SourceBuildArtifact) -> SchemaBuildResult:
    views = dict(artifact.view_ddls)
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
        state=raw.get("state", "active"),
        enabled=raw.get("enabled", True),
        current=raw.get("current", "1.0.0"),
        deployed_version=raw.get("deployed_version"),
        versions=list(raw.get("versions") or []),
        updated_at=raw.get("updated_at") or "",
        header_type=raw.get("header_type"),
        has_transform=bool(raw.get("has_transform")),
        has_fetcher=bool(raw.get("has_fetcher")),
        views=list(raw.get("views") or []),
    )


def _normalize_attribute(value: Any) -> list[str] | None:
    """Match meta-schema column ``attribute``: list[str] | None."""
    if value is None or value == [] or value == "":
        return None
    if isinstance(value, str):
        return [value]
    return list(value)


def _to_api_schema_column(col: Any) -> SchemaColumn:
    return SchemaColumn(
        name=col.name,
        type=col.type,
        use_case=getattr(col, "use_case", "") or "",
        attribute=_normalize_attribute(getattr(col, "attribute", None)),
        description=getattr(col, "comment", None) or getattr(col, "description", "") or "",
    )
