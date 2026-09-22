"""Sources router -- CRUD, pagination, search, sort, bulk operations.

GET    /api/v1/sources                  -> Paginated source list
POST   /api/v1/sources                  -> Create source
GET    /api/v1/sources/catalogue        -> The sources a deployed transform already handles
POST   /api/v1/sources/from-catalogue/{entry} -> Create a source from a catalogue entry
GET    /api/v1/sources/{name}           -> Get source details
GET    /api/v1/sources/{name}/flow      -> The stages its records travel, resolved
GET    /api/v1/sources/{name}/export    -> One version as a portable bundle
POST   /api/v1/sources/import           -> Apply a bundle from another deployment
GET    /api/v1/sources/{name}/versions/{version}  -> Get one version snapshot
GET    /api/v1/sources/{name}/columns   -> Composed schema columns for a version
POST   /api/v1/sources/{name}/build     -> Build DDL from a version snapshot
POST   /api/v1/sources/{name}/plan      -> Dry-run deploy plan (not persisted)
POST   /api/v1/sources/{name}/deploy    -> Deploy version to ClickHouse
PUT    /api/v1/sources/{name}           -> Update source
PATCH  /api/v1/sources/{name}           -> Enable or disable source
DELETE /api/v1/sources/{name}           -> Delete source
POST   /api/v1/sources/bulk             -> Bulk enable/disable/delete
POST   /api/v1/sources/seed             -> Seed built-in defaults
POST   /api/v1/sources/reconcile-apps   -> Bring the derived app state into step with the sources

Every write that changes what the apps must do -- a deploy, a state change, a
delete, an edit -- ends by reconciling the deploy repo: the receiver and loader
routing blocks are recompiled, and a fetcher-based source gains or loses its
fetcher instance. The reconcile never fails the source write; its outcome is
reported, and ``reconcile-apps`` retries it.

A deploy also points HyperDX at the table it just made, and a delete takes that
source away again. Like the reconcile, neither ever fails the source write.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, NoReturn

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from scalo.logger import logger

from dfe_engine.api.deps import (
    ClickHouseClient,
    CurrentUser,
    SchemaReg,
    Settings,
    SourceReg,
    require_action,
)
from dfe_engine.api.errors import (
    CoreResourceConflictErrorResponse,
    ErrorResponse,
    SourceCreateConflictResponse,
    SourceWriteConflictResponse,
    raise_exchange_http,
)
from dfe_engine.api.pagination import PaginatedResponse, PaginationParams, apply_search, apply_sort
from dfe_engine.api.v1.apps import commit_overlay, remove_overlay
from dfe_engine.appmgmt import (
    LOADER_COMPILER,
    MetricsUnavailableError,
    OperationalReader,
    appconfig,
    derived,
    instances,
)
from dfe_engine.auth.audit import audit_resource_change
from dfe_engine.auth.rbac_scopes import scopes_dict
from dfe_engine.exchange.models import SourceBundle, SourceImportResult
from dfe_engine.exchange.schemas import ExchangeError
from dfe_engine.exchange.sources import apply_source_bundle, build_source_bundle
from dfe_engine.git_identity import git_author
from dfe_engine.manifest import ManifestError
from dfe_engine.settings import get_settings
from dfe_engine.source import catalogue as source_catalogue_module
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
from dfe_engine.source.flow import FlowError, SourceFlow, resolve_flow
from dfe_engine.source.models import (
    DEFAULT_LANDING_LABEL,
    PaginatedSourceSummaryResponse,
    Source,
    SourceState,
    SourceSummaryObject,
    SourceVersion,
    SourceVersionGetResponse,
    SourceWriteRequest,
    engine_registry,
)
from dfe_engine.source.registry import (
    SourceCoreResourceError,
    SourceMatchConflictError,
    SourceNotFoundError,
    SourceValidationError,
)
from dfe_engine.transport import SourceTransport

router = APIRouter(prefix="/sources", tags=["Sources"])


def _raise_save_validation_http(exc: SourceValidationError) -> NoReturn:
    """Map registry validation errors to HTTP responses (never 500)."""
    # 409, matching the core-resource guard's answer for roles, schemas and field maps.
    if isinstance(exc, SourceCoreResourceError):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "conflict",
                "message": str(exc),
                "source": exc.source,
            },
        ) from exc
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


def _failure_code(exc: Exception) -> str:
    """The code a bulk entry reports, so one refusal reads the same on either route.

    Ordered narrowest first: the two conflict errors both subclass
    ``SourceValidationError``.
    """
    if isinstance(exc, SourceCoreResourceError):
        return "conflict"
    if isinstance(exc, SourceMatchConflictError):
        return "match_conflict"
    if isinstance(exc, SourceNotFoundError):
        return "not_found"
    if isinstance(exc, SourceValidationError):
        return "validation_error"
    return "internal_error"


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
    apps_synced: list[str] = Field(
        default_factory=list,
        description="Deploy-repo writes made so the apps follow this change (service/instance: action)",
    )
    apps_sync_error: str | None = Field(
        default=None,
        description="Why the apps could not be brought into step; reconcile-apps retries it",
    )
    restart_required: list[str] = Field(
        default_factory=list,
        description=(
            "One command per app whose running process cannot take this change where "
            "it stands. Empty where every write was hot, or where a GitOps controller "
            "rolls the pod itself."
        ),
    )


class SourceImportResponse(SourceImportResult):
    """A bundle applied, plus what the apps now need to follow it."""

    apps_synced: list[str] = Field(
        default_factory=list,
        description="Deploy-repo writes made so the apps follow the imported source",
    )
    apps_sync_error: str | None = Field(
        default=None,
        description="Why the apps could not be brought into step; reconcile-apps retries it",
    )
    restart_required: list[str] = Field(
        default_factory=list,
        description="One command per app whose running process cannot take this change in place",
    )


class AppsReconcileResponse(BaseModel):
    """What the reconcile wrote into the deploy repo."""

    changes: list[str] = Field(
        default_factory=list, description="Overlay writes made (service/instance: action)"
    )
    restart_required: list[str] = Field(
        default_factory=list,
        description=(
            "One command per app whose running process cannot take this change where it stands."
        ),
    )


@dataclass(frozen=True, slots=True)
class AppsSync:
    """What bringing the apps into step with the sources did, and what it now needs."""

    changes: list[str]
    error: str | None = None
    restart_required: list[str] = field(default_factory=list)


def _source_response(
    source: Source,
    *,
    message: str,
    apps: AppsSync | None = None,
) -> SourceResponse:
    synced = apps or AppsSync(changes=[])
    return SourceResponse(
        source=source.source,
        message=message,
        current=source.current,
        deployed_version=source.deployed_version,
        versions=sorted(source.versions.keys()),
        apps_synced=synced.changes,
        apps_sync_error=synced.error,
        restart_required=synced.restart_required,
    )


def _one_per_app(hints: list[str]) -> list[str]:
    """The restart hints in the order first reported, with repeats dropped.

    Every write renders every app, so two overlays written in one reconcile can
    each report the same app; the caller runs one command per app.
    """
    return list(dict.fromkeys(hints))


def _reconcile_apps(request: Request, user: Any, registry: Any) -> AppsSync:
    """Apply what the sources imply about the deployed apps. Never raises.

    Returns the writes made, why the reconcile could not complete when it did
    not, and the restart each app needs where this deployment renders its config
    files itself. A deployment without a deploy repo has nothing to reconcile.

    The hints are collected from the writes as well as from the render that
    follows them, because the overlay write renders the app's config on its way
    through: by the time the reconcile renders again the file is already current,
    so an app whose config only that write changed would be reported as needing
    nothing and a Compose stack would leave it on the source it started with.
    """
    gc = getattr(request.app.state, "gitcrud", None)
    if gc is None:
        return AppsSync(changes=[])
    settings = request.app.state.settings
    done: list[str] = []
    hints: list[str] = []
    try:
        for change in derived.plan(gc, registry, settings):
            if change.action == "remove":
                written = remove_overlay(request, user, change.app)
            else:
                written = commit_overlay(
                    request, user, change.app, change.doc or {}, summary=change.summary
                )
            # Each write renders the app config itself, so its hint is the only
            # report of that change: the render below finds the file current.
            hints += written.restart_required
            done.append(change.describe())
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, str) else exc.detail.get("message", "")
        logger.warning(f"apps not reconciled with the sources: {detail}")
        return AppsSync(changes=done, error=str(detail), restart_required=_one_per_app(hints))
    except Exception as exc:
        logger.warning(f"apps not reconciled with the sources: {exc}")
        return AppsSync(changes=done, error=str(exc), restart_required=_one_per_app(hints))
    # Re-rendered even when the reconcile wrote nothing: a source deploy changes
    # the loader's table map through the same overlay the plan found in step.
    hints += appconfig.render_and_report(gc, settings)
    return AppsSync(changes=done, restart_required=_one_per_app(hints))


async def _sync_hyperdx_source(
    request: Request, source: Source, database: str, columns: list[str]
) -> tuple[int | None, str | None]:
    """Point HyperDX at the table this deploy just made. Never raises.

    Returns how many HyperDX teams now carry the source and, when nothing was
    written, why. A deployment without HyperDX has nothing to point at.
    """
    client = getattr(request.app.state, "hyperdx_client", None)
    if client is None:
        return None, None

    from dfe_engine.hyperdx.sources import ensure_source

    try:
        teams = await ensure_source(
            client,
            name=source.source,
            database=database,
            table=source.table_name,
            columns=columns,
        )
    except Exception as exc:
        logger.warning(f"HyperDX not pointed at source '{source.source}': {exc}")
        return None, str(exc)
    if teams is None:
        return None, "HyperDX did not accept the source; see the engine log"
    return len(teams), None


async def _remove_hyperdx_source(request: Request, name: str) -> None:
    """Drop the HyperDX source for a DFE source that is going away. Never raises."""
    client = getattr(request.app.state, "hyperdx_client", None)
    if client is None:
        return

    from dfe_engine.hyperdx.sources import remove_source

    try:
        await remove_source(client, name=name)
    except Exception as exc:
        logger.warning(f"HyperDX source not removed for '{name}': {exc}")


def _resolve_source(name: str, registry: Any) -> Source:
    """One source, or 404."""
    try:
        return registry.get_source(name)
    except SourceNotFoundError:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Source {name!r} not found"},
        ) from None


def _landing_table(source: Source) -> tuple[str, bool]:
    """The table this source's records land in, and whether that is the shared default.

    The loader takes the table from the ``_source`` label the producer stamped,
    and the ``source_to_table`` map the compile emits is the identity - so the
    table IS the landing label. A fetcher pushing to the main topic therefore
    lands in the platform's landing table rather than in one of its own name.
    """
    label = source.landing_label()
    return label, label == DEFAULT_LANDING_LABEL


def _deployed_loaders(request: Request) -> list[str]:
    """The OTel service names of the deployed instances that count records per table.

    Read from the manifest's routing compiler rather than from an app name, so an
    org that renames or replaces the loading stage is measured just the same.
    """
    gc = getattr(request.app.state, "gitcrud", None)
    if gc is None:
        return []
    return [i.telemetry_name for i in instances.instances_routed_by(gc, LOADER_COMPILER)]


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
    failed: list[dict[str, str]] = Field(
        default_factory=list,
        description=(
            "One entry per source left untouched: its name, the same code the "
            "single-source route answers with, and the message"
        ),
    )


class SeedResponse(BaseModel):
    """Result of seeding built-in sources."""

    seeded: int = Field(description="Number of sources seeded")


class TableEngineObject(BaseModel):
    """One table engine a source may select, as the console offers it."""

    name: str = Field(description="MergeTree-family variant, e.g. ReplacingMergeTree")
    description: str = Field(description="What the engine does with rows")
    arguments: Literal["none", "optional", "required"] = Field(
        description="Whether the variant takes arguments inside its parentheses"
    )
    argument_hint: str = Field(
        description="What goes inside the parentheses; empty when arguments is none"
    )


class CatalogueEntryObject(BaseModel):
    """One source a deployed transform already handles, as the console lists it."""

    name: str = Field(description="The shipping app's key for this source")
    package: str = Field(description="Vendor integration package it came from")
    data_stream: str = Field(description="Data stream within that package")
    dataset: str = Field(description="package.data_stream - what a Beats event stamps")
    intakes: list[str] = Field(description="Ways this source's payload can reach the platform")
    framing: str | None = Field(
        default=None,
        description="Pushed intakes only: whether the pipeline wants the syslog line or the body",
    )
    transforms: list[str] = Field(description="Programs the app compiled for this source")
    beats: dict[str, str] = Field(
        default_factory=dict,
        description="Beats module and fileset carrying the same source, when one does",
    )
    source: str = Field(
        default="",
        description=(
            "Source name this entry derives; empty when the entry's own name is not a "
            "legal source name and one must be supplied on create"
        ),
    )


class CatalogueSourceRequest(BaseModel):
    """Create a source from a catalogue entry."""

    model_config = ConfigDict(extra="forbid")

    intake: str = Field(description="How this source's data arrives: beats, receiver or fetcher")
    name: str | None = Field(
        default=None,
        description="Source name; defaults to the entry's own name as a Kubernetes label",
    )
    transform: str = Field(
        default=source_catalogue_module.DEFAULT_TRANSFORM,
        description="Which of the entry's transforms this source runs",
    )
    transport: SourceTransport | None = Field(
        default=None,
        description="bus or direct; omitted takes the deployment default",
    )
    archive: bool = Field(
        default=False,
        description="Keep the raw record as it arrived; needs the bus transport",
    )


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
    views: dict[str, str] = Field(default_factory=dict, description="View name -> DDL")


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
        description="Version id -> configuration snapshot and pipeline artifacts",
    )


class FlowTransformModel(BaseModel):
    """The transform stage, when the source has one."""

    app: str = Field(description="Catalogued app running it, e.g. dfe-transform-vrl")
    instance: str = Field(description="Its deployed name, e.g. dfe-transform-vrl-auth")
    variant: str | None = Field(
        default=None,
        description="The compiled-in program it runs, where the app offers a catalogue of them",
    )
    endpoint: str | None = Field(
        default=None, description="Direct: the address records reach it on. Null on the bus."
    )
    topics: list[str] | None = Field(
        default=None, description="Bus: the landing and transformed topics. Null on direct."
    )


class FlowOutputsModel(BaseModel):
    """Where the records end up."""

    loader: str = Field(
        description="The topic the loader consumes, or the endpoint it is pushed to"
    )
    archive: bool = Field(
        description="Whether the archiver also keeps the raw record off the landing topic"
    )


class SourceFlowResponse(BaseModel):
    """One source's whole path, as the resolver reports it.

    The wire shape of ``dfe_engine.source.flow.SourceFlow``, and the only copy of
    it: everything here is built by ``_flow_response`` from that dataclass, so a
    stage the resolver gains is a field added in one place.
    """

    source: str
    transport: str = Field(
        description="bus: a broker holds records between stages; direct: no store"
    )
    carrier: str = Field(
        description="What carries it here - the bus provider, or the direct protocol"
    )
    origin: str | None = Field(
        default=None,
        description="receiver, fetcher, or null when nothing selects the records",
    )
    input: str = Field(
        description="The receiver match that selects the records, or the fetcher instance polling them"
    )
    transform: FlowTransformModel | None = None
    outputs: FlowOutputsModel
    table: str


def _flow_response(flow: SourceFlow) -> SourceFlowResponse:
    """The resolver's answer on the wire."""
    return SourceFlowResponse(
        source=flow.source,
        transport=flow.transport,
        carrier=flow.carrier,
        origin=flow.origin,
        input=flow.input,
        transform=(
            FlowTransformModel(
                app=flow.transform.app,
                instance=flow.transform.instance,
                variant=flow.transform.variant,
                endpoint=flow.transform.endpoint,
                topics=list(flow.transform.topics) if flow.transform.topics else None,
            )
            if flow.transform
            else None
        ),
        outputs=FlowOutputsModel(loader=flow.outputs.loader, archive=flow.outputs.archive),
        table=flow.table,
    )


class SourceSignalsResponse(BaseModel):
    """What this source's records are actually doing, beside its definition.

    A reading is null when the window holds nothing to compute it from, and the
    console hides a null rather than showing a zero: an absent series means "not
    measurable here", which is a different statement from "no records".
    """

    source: str
    table: str = Field(description="The ClickHouse table these records land in.")
    landed_in_default: bool = Field(
        description=(
            "True when that table is the platform's default landing table, which "
            "every unmatched record shares. The loader counts records per TABLE, "
            "so the rate below then covers the whole table rather than this source "
            "alone."
        )
    )
    window_seconds: int = Field(description="How far back the readings look.")
    records_per_min: float | None = Field(
        description="Records a minute across the window; null when no series answers."
    )
    last_seen: str | None = Field(
        description=(
            "UTC ISO-8601 timestamp of the last increase in the window; null when "
            "the counter never moved. A flat counter keeps reporting after traffic "
            "stops, so a present series is not itself an arrival."
        )
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
    """List sources with pagination, search, filtering, and a full object tree.

    The landing source (``main``) is always the first item when it is in the
    result set; remaining sources keep the requested sort.
    """
    raw_sources = registry.list_sources(enabled_only=bool(enabled))

    if enabled is False:
        raw_sources = [s for s in raw_sources if not s.get("enabled", True)]

    raw_sources = apply_search(raw_sources, search, ["source", "display_name", "description"])
    raw_sources = apply_sort(raw_sources, sort_by, sort_order)
    raw_sources = _pin_landing_source_first(raw_sources)

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
                "Source name already exists (code conflict), the name is the core "
                "landing the engine owns and reconciles itself (code conflict), or "
                "receiver match duplicates another enabled source (code match_conflict)"
            ),
        },
    },
    dependencies=[Depends(require_action(scopes_dict["source_write"]))],
)
async def create_source(
    body: SourceWriteRequest,
    user: CurrentUser,
    registry: SourceReg,
    request: Request,
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
    return _source_response(
        source, message="created", apps=_reconcile_apps(request, user, registry)
    )


@router.get(
    "/catalogue",
    response_model=PaginatedResponse[CatalogueEntryObject],
    dependencies=[Depends(require_action(scopes_dict["source_read"]))],
)
async def list_catalogue(
    user: CurrentUser,
    pagination: PaginationParams = Depends(),
    intake: str | None = Query(
        None, description="Only entries that arrive this way (beats, receiver, fetcher)"
    ),
    search: str | None = Query(None, description="Search the entry name, package and data stream"),
) -> PaginatedResponse[CatalogueEntryObject]:
    """List the sources the deployed transforms already handle.

    Empty when no catalogue is mounted, which is a deployment without one rather
    than an error: the catalogue is a release asset of the app that ships it.
    """
    catalogue = _source_catalogue()
    if catalogue is None:
        return PaginatedResponse.from_list([], pagination.page, pagination.per_page)
    if intake is not None and intake not in source_catalogue_module.INTAKES:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "validation_error",
                "message": (
                    f"Unknown intake {intake!r}. Must be: "
                    f"{', '.join(sorted(source_catalogue_module.INTAKES))}"
                ),
            },
        )

    rows = [
        _to_catalogue_object(entry)
        for entry in catalogue.entries.values()
        if intake is None or intake in entry.intakes
    ]
    if search:
        needle = search.lower()
        rows = [
            row
            for row in rows
            if needle in row.name.lower()
            or needle in row.package.lower()
            or needle in row.data_stream.lower()
        ]
    rows.sort(key=lambda row: row.name)
    return PaginatedResponse.from_list(rows, pagination.page, pagination.per_page)


@router.get(
    "/engines",
    response_model=PaginatedResponse[TableEngineObject],
    dependencies=[Depends(require_action(scopes_dict["source_read"]))],
)
async def list_table_engines(
    user: CurrentUser,
    pagination: PaginationParams = Depends(),
) -> PaginatedResponse[TableEngineObject]:
    """List the table engines a source may select, from dfe-schemas ``registries/engines.yaml``."""
    rows = [
        TableEngineObject(
            argument_hint=option.argument_hint,
            arguments=option.arguments,
            description=option.description,
            name=option.name,
        )
        for option in engine_registry().options
    ]
    return PaginatedResponse.from_list(rows, pagination.page, pagination.per_page)


@router.post(
    "/from-catalogue/{entry}",
    response_model=SourceResponse,
    status_code=201,
    responses={
        409: {
            "model": SourceCreateConflictResponse,
            "description": (
                "A source of that name already exists, the name is the core landing "
                "the engine owns and reconciles itself, or its match duplicates another"
            ),
        },
    },
    dependencies=[Depends(require_action(scopes_dict["source_write"]))],
)
async def create_source_from_catalogue(
    entry: str,
    body: CatalogueSourceRequest,
    user: CurrentUser,
    registry: SourceReg,
    settings: Settings,
    request: Request,
):
    """Create a source from a catalogue entry, on the intake it arrives by.

    The entry supplies the match rule or the fetcher family, the transform
    variant and the shipped meta schema; everything after that is the ordinary
    create, so the source is indistinguishable from a hand-written one.
    """
    catalogue = _source_catalogue()
    if catalogue is None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "not_configured",
                "message": "no source catalogue is mounted on this deployment",
            },
        )
    try:
        write = source_catalogue_module.write_request_for(
            catalogue,
            catalogue.entry(entry),
            intake=body.intake,
            settings=settings,
            name=body.name,
            transform=body.transform,
            transport=body.transport,
            archive=body.archive,
        )
    except source_catalogue_module.SourceCatalogueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "catalogue_error", "message": str(exc)},
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "validation_error", "message": str(exc)},
        ) from exc
    return await create_source(write, user, registry, request)


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
        resource_type=source.resource_type,
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
        default_ttl_days=settings.clickhouse.default_ttl_days,
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

    Runs the v2 YAML -> DDL pipeline and returns the generated DDL
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
    from dfe_engine.schema.engine_resolver import EngineResolver
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
    # The plan shows the DDL the deploy would apply, so it senses the engine and
    # ON CLUSTER from the same server.
    resolver = EngineResolver(client=ch_client, topology_setting=settings.clickhouse.topology)
    try:
        result, _artifact = ensure_build_artifact(
            store,
            source,
            version_id=version_id,
            schemas_base_dir=settings.schemas.schemas_dir or None,
            refresh=True,
            resolver=resolver,
        )
    except SchemaBuildError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "build_error", "message": str(exc)},
        ) from exc

    builder = SchemaBuilderV2(
        TypeRegistry.default(),
        schemas_base_dir=settings.schemas.schemas_dir or None,
        default_engine=settings.clickhouse.default_engine,
        default_ttl_days=settings.clickhouse.default_ttl_days,
        resolver=resolver,
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


def _ensure_source_topics(
    source: Any, settings: Any, version_id: str | None = None
) -> tuple[list[str], list[str]]:
    """Create the ``_land``/``_load`` topics this source needs.

    Deliberately non-fatal: the schema is already live, and a brokerless profile
    has no bus at all, where failing the deploy would be wrong. Failures are
    reported on the response instead.

    Blocking librdkafka calls run in here, so callers on the event loop hand it to
    a thread. ``version_id`` is the version being deployed, not the deployed one.
    """
    from dfe_engine.kafka.topics import (
        deployment_topic_config,
        ensure_topics,
        source_topic_specs,
        topics_managed,
    )

    if not topics_managed(settings):
        return [], []

    specs = source_topic_specs(
        source,
        partitions=settings.kafka.topic_partitions,
        replication_factor=settings.kafka.topic_replication_factor,
        config=deployment_topic_config(settings),
        version_id=version_id,
    )
    outcome = ensure_topics(specs, settings=settings)
    if outcome.failed:
        logger.warning(
            f"Kafka topics not ensured for source '{source.source}': "
            f"{', '.join(f'{n} ({e})' for n, e in outcome.failed)}"
        )
    return outcome.created + outcome.existing, [name for name, _ in outcome.failed]


def _remove_source_topics(source: Any, settings: Any) -> tuple[list[str], list[str]]:
    """Delete the ``_land``/``_load`` topics this source's deploys created.

    The same dial that creates them removes them: an engine that manages a
    source's topics manages them for the source's whole life, and leaving them
    costs a partition assignment in the loader for a source nothing can write to.

    Non-fatal like the ensure, and blocking, so a caller on the event loop hands
    it to a thread. Returns the topics removed and the ones that could not be.
    """
    from dfe_engine.kafka.topics import remove_topics, source_topic_names, topics_managed

    if not topics_managed(settings):
        return [], []

    outcome = remove_topics(source_topic_names(source), settings=settings)
    if outcome.failed:
        logger.warning(
            f"Kafka topics not removed for source '{source.source}': "
            f"{', '.join(f'{n} ({e})' for n, e in outcome.failed)}"
        )
    return outcome.removed, [name for name, _ in outcome.failed]


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
    request: Request,
    version: str | None = Query(
        None, description="Source version id (defaults to the source's current version)"
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

    # Targets current: defaulting to the deployed version makes an unparameterised
    # deploy a no-op that can never advance.
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

    # A deploy reaches ClickHouse before the build: the table engine and ON CLUSTER
    # are sensed from the live server, so the DDL lands on every replica of a
    # cluster. A dry run stays CH-free and renders the deployment's topology.
    ch = None
    resolver = None
    if not dry_run:
        from dfe_engine.clickhouse.clickhouse_manager import ClickHouseManager
        from dfe_engine.schema.engine_resolver import EngineResolver
        from dfe_engine.settings import get_clickhouse_config

        try:
            ch = ClickHouseManager.get_instance(
                get_clickhouse_config(settings)
            ).get_clickhouse_client()
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail={"code": "clickhouse_unavailable", "message": str(exc)},
            ) from exc
        resolver = EngineResolver(client=ch, topology_setting=settings.clickhouse.topology)

    builder = SchemaBuilderV2(
        TypeRegistry.default(),
        schemas_base_dir=settings.schemas.schemas_dir or None,
        default_engine=settings.clickhouse.default_engine,
        default_ttl_days=settings.clickhouse.default_ttl_days,
        resolver=resolver,
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
        # Through the applier on the sensed resolver: a bare CREATE DATABASE lands
        # on one replica, leaving the ON CLUSTER table DDL nowhere to go.
        from dfe_engine.schema.applier import SchemaApplier

        SchemaApplier(ch, resolver).ensure_database(db)
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

    # Off the event loop: the admin calls block for their full timeout when no
    # broker answers, which is the norm on the Kafka-less profile.
    topics_ensured, topics_failed = await asyncio.to_thread(
        _ensure_source_topics, source, settings, version_id
    )

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
        topics_ensured=topics_ensured,
        topics_failed=topics_failed,
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

    # The receiver's rule for this source, the loader's table map and (for a
    # fetcher-based source) the fetcher instance are what make the deploy live.
    apps = _reconcile_apps(request, user, registry)

    # The HyperDX source is how an operator sees the rows the new table takes;
    # without it the deploy lands and stays invisible until someone adds one.
    hyperdx_teams, hyperdx_error = await _sync_hyperdx_source(
        request, source, db, [col.name for col in result.columns]
    )

    deploy_result = deploy_result.model_copy(
        update={
            "apps_synced": apps.changes,
            "apps_sync_error": apps.error,
            "restart_required": apps.restart_required,
            "hyperdx_source_teams": hyperdx_teams,
            "hyperdx_source_error": hyperdx_error,
        }
    )
    store.save_deploy(deploy_result, source)

    # Topic creation mutates the broker, so it is attributable and belongs in the
    # audit record alongside the DDL rather than only in an unattributed log line.
    audit_resource_change(
        user.user_id,
        "schema",
        name,
        "deployed",
        details={
            "version": version_id,
            "topics_ensured": topics_ensured,
            "topics_failed": topics_failed,
            "apps_synced": apps.changes,
        },
    )
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


@router.get(
    "/{name}/flow",
    response_model=SourceFlowResponse,
    responses={
        422: {
            "description": (
                "The source's stages cannot be run as declared - the message names "
                "which stage refused and why"
            )
        },
    },
    dependencies=[Depends(require_action(scopes_dict["source_read"]))],
)
async def get_source_flow(name: str, user: CurrentUser, registry: SourceReg, settings: Settings):
    """The stages this source's records travel, resolved against this deployment.

    The console draws the flow from this rather than from the source's fields,
    for the same reason the compilers write from it: the topics, the endpoints
    and the instance running each stage follow from the source plus the
    deployment, and one resolver is what keeps the drawing and the deployed
    config the same answer.
    """
    source = _resolve_source(name, registry)
    try:
        return _flow_response(resolve_flow(source, settings))
    except FlowError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "flow_error", "message": str(exc)},
        ) from exc


@router.get(
    "/{name}/signals",
    response_model=SourceSignalsResponse,
    dependencies=[Depends(require_action(scopes_dict["source_read"]))],
)
async def get_source_signals(
    name: str,
    user: CurrentUser,
    request: Request,
    registry: SourceReg,
    settings: Settings,
    client: ClickHouseClient,
) -> SourceSignalsResponse:
    """Whether this source's records are arriving, and how fast.

    Reads the loader's per-table counter out of the otel tables over a bounded
    five-minute window, so the console can put a number beside a source without
    the operator opening HyperDX. It answers 200 with nulls where the series is
    absent: a source that was only just defined has nothing to report, and that
    is not an error.
    """
    source = _resolve_source(name, registry)
    table, in_default = _landing_table(source)
    loaders = _deployed_loaders(request)
    try:
        signals = OperationalReader(
            client, settings.clickhouse.effective_data_database
        ).source_signals(table, loaders)
    except MetricsUnavailableError as exc:
        raise HTTPException(
            status_code=503, detail={"code": "metrics_unavailable", "message": str(exc)}
        ) from exc
    return SourceSignalsResponse(
        source=source.source,
        table=table,
        landed_in_default=in_default,
        window_seconds=signals.window_seconds,
        records_per_min=signals.records_per_min,
        last_seen=(
            datetime.fromtimestamp(signals.last_seen_epoch, tz=UTC).isoformat()
            if signals.last_seen_epoch is not None
            else None
        ),
    )


@router.get(
    "/{name}/export",
    response_model=SourceBundle,
    response_model_exclude_none=True,
    dependencies=[Depends(require_action(scopes_dict["source_read"]))],
)
async def export_source(
    name: str,
    user: CurrentUser,
    registry: SourceReg,
    schema_registry: SchemaReg,
    version: str | None = Query(
        None, description="Version id to export; omitted takes the source's current"
    ),
) -> SourceBundle:
    """Export one source version as a bundle another deployment can import.

    The bundle carries the version's routing, schema and transform, each only
    where the source declares it, plus one document per meta schema the schema
    pins name. A pinned schema with ``resource_type: core`` travels as a
    REFERENCE and a version pin rather than a copy of its columns, so importing
    the bundle cannot fork the read-only definition dfe-schemas ships.
    """
    _resolve_source(name, registry)
    try:
        return build_source_bundle(registry, schema_registry, name, version=version)
    except ExchangeError as exc:
        raise_exchange_http(exc)


@router.put(
    "/{name}",
    response_model=SourceResponse,
    responses={
        409: {
            "model": SourceWriteConflictResponse,
            "description": (
                "Receiver match duplicates another enabled source (code match_conflict), "
                "or the source is engine-owned and no write path may change it (code conflict)"
            ),
        },
    },
    dependencies=[Depends(require_action(scopes_dict["source_write"]))],
)
async def update_source(
    name: str,
    body: SourceWriteRequest,
    user: CurrentUser,
    registry: SourceReg,
    request: Request,
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
    return _source_response(
        source, message="updated", apps=_reconcile_apps(request, user, registry)
    )


@router.patch(
    "/{name}",
    response_model=SourceResponse,
    responses={
        409: {
            "model": SourceWriteConflictResponse,
            "description": (
                "Enabling would duplicate another enabled source's receiver match (code "
                "match_conflict), or the source is engine-owned and no write path may "
                "change it (code conflict)"
            ),
        },
    },
    dependencies=[Depends(require_action(scopes_dict["source_write"]))],
)
async def patch_source_enabled(
    name: str,
    body: SourceEnabledPatchRequest,
    user: CurrentUser,
    registry: SourceReg,
    request: Request,
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
    return _source_response(saved, message=target, apps=_reconcile_apps(request, user, registry))


@router.delete(
    "/{name}",
    status_code=204,
    responses={
        409: {
            "model": CoreResourceConflictErrorResponse,
            "description": "The source is engine-owned and no write path may delete it",
        },
    },
    dependencies=[Depends(require_action(scopes_dict["source_delete"]))],
)
async def delete_source(
    name: str, user: CurrentUser, registry: SourceReg, settings: Settings, request: Request
):
    """Delete a source by name. Its fetcher instance, receiver rule and topics go with it.

    An engine-owned source -- the landing table's own -- is refused with 409
    ``conflict``, the same answer PUT and PATCH give.
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
    try:
        registry.delete_source(name, created_by=git_author(user))
    except SourceValidationError as e:
        _raise_save_validation_http(e)
    # Off the event loop: the admin calls block for their full timeout when no
    # broker answers, which is the norm on the Kafka-less profile.
    topics_removed, topics_failed = await asyncio.to_thread(_remove_source_topics, source, settings)
    # Deleting a topic destroys what is on it, so it is attributable and belongs
    # in the audit record rather than only in a log line.
    audit_resource_change(
        user.user_id,
        "source",
        name,
        "deleted",
        details={"topics_removed": topics_removed, "topics_failed": topics_failed},
    )
    _reconcile_apps(request, user, registry)
    await _remove_hyperdx_source(request, name)


@router.post(
    "/bulk",
    response_model=BulkActionResponse,
    dependencies=[Depends(require_action(scopes_dict["source_write"]))],
)
async def bulk_action(
    body: BulkActionRequest,
    user: CurrentUser,
    registry: SourceReg,
    settings: Settings,
    request: Request,
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
    deleted: list[Source] = []

    for name in body.sources:
        try:
            if body.action == "delete":
                # Read before the delete, for the topics it owns. A name with no
                # stored source still deletes, as it did before it had topics.
                stored = registry.get_source(name) if registry.source_exists(name) else None
                registry.delete_source(name, created_by=git_author(user))
                if stored is not None:
                    deleted.append(stored)
            else:
                source = registry.get_source(name)
                updated = source.model_copy(update={"state": action_to_state[body.action]})
                registry.save_source(updated, created_by=git_author(user))
            succeeded.append(name)
        except Exception as e:
            failed.append({"source": name, "code": _failure_code(e), "error": str(e)})

    result = BulkActionResponse(action=body.action, succeeded=succeeded, failed=failed)
    if succeeded:
        audit_resource_change(user.user_id, "source", ",".join(succeeded), body.action)
        _reconcile_apps(request, user, registry)
        for source in deleted:
            # One pass per source: the delete is already done, and a broker that
            # cannot be reached must not cost the next source its cleanup.
            await asyncio.to_thread(_remove_source_topics, source, settings)
        if body.action == "delete":
            for name in succeeded:
                await _remove_hyperdx_source(request, name)
    return result


@router.post(
    "/reconcile-apps",
    response_model=AppsReconcileResponse,
    dependencies=[Depends(require_action(scopes_dict["source_write"]))],
)
async def reconcile_apps(user: CurrentUser, registry: SourceReg, request: Request):
    """Bring the deploy repo's derived app state into step with the sources.

    Recompiles every stack-scoped routing block (receiver, loader, archiver) and
    deploys, syncs or removes the instances of every instance-scoped app (a fetcher
    per active fetcher-based source). Every source write does this on its own; this
    route is the retry when one reported ``apps_sync_error``.
    """
    if getattr(request.app.state, "gitcrud", None) is None:
        raise HTTPException(
            status_code=503,
            detail={"code": "not_configured", "message": "gitops is not enabled"},
        )
    apps = _reconcile_apps(request, user, registry)
    if apps.error:
        raise HTTPException(
            status_code=502,
            detail={"code": "reconcile_failed", "message": apps.error, "changes": apps.changes},
        )
    return AppsReconcileResponse(changes=apps.changes, restart_required=apps.restart_required)


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


@router.post(
    "/import",
    response_model=SourceImportResponse,
    status_code=201,
    responses={
        409: {
            "model": ErrorResponse,
            "description": (
                "The source name already exists here, or a meta schema the bundle carries "
                "would land on an occupied path (code conflict)"
            ),
        },
    },
    dependencies=[Depends(require_action(scopes_dict["source_write"]))],
)
async def import_source(
    body: SourceBundle,
    user: CurrentUser,
    registry: SourceReg,
    schema_registry: SchemaReg,
    request: Request,
) -> SourceImportResponse:
    """Import a source bundle exported from another deployment.

    The bundle's meta-schema definitions are written first, then the source, both
    through the registries that commit to git. A definition marked
    ``resource_type: core`` is resolved against what dfe-schemas already put here
    and never written, so the read-only schema is not forked.

    Every check runs before the first write: a bundle applied halfway would leave
    the source's schema pins with no definitions behind them.
    """
    try:
        result = apply_source_bundle(
            registry,
            schema_registry,
            body,
            created_by=git_author(user),
        )
    except ExchangeError as exc:
        raise_exchange_http(exc)

    if result.action == "resolved":
        return SourceImportResponse(**result.model_dump())

    audit_resource_change(user.user_id, "source", result.source, "created")
    apps = _reconcile_apps(request, user, registry)
    return SourceImportResponse(
        **result.model_dump(),
        apps_synced=apps.changes,
        apps_sync_error=apps.error,
        restart_required=apps.restart_required,
    )


# ── Helpers ──────────────────────────────────────────────────


def _utc_now_iso() -> str:

    return datetime.now(tz=UTC).isoformat()


def _source_catalogue() -> source_catalogue_module.SourceCatalogue | None:
    """The mounted catalogue, or a 502 naming what is wrong with it.

    A missing catalogue is None and the routes answer empty; a catalogue that is
    there but unreadable is a deployment fault worth saying out loud.
    """
    try:
        return source_catalogue_module.source_catalogue()
    except ManifestError as exc:
        raise HTTPException(
            status_code=502,
            detail={"code": "catalogue_error", "message": str(exc)},
        ) from exc


def _to_catalogue_object(
    entry: source_catalogue_module.CatalogueEntry,
) -> CatalogueEntryObject:
    try:
        derived = entry.source_name()
    except ValueError:
        derived = ""
    return CatalogueEntryObject(
        name=entry.name,
        package=entry.package,
        data_stream=entry.data_stream,
        dataset=entry.dataset,
        intakes=sorted(entry.intakes),
        framing=entry.framing,
        transforms=list(entry.transforms),
        beats=dict(entry.beats),
        source=derived,
    )


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


def _pin_landing_source_first(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the landing source (``main``) first; other rows keep their order."""
    landing = [row for row in rows if row.get("source") == DEFAULT_LANDING_LABEL]
    rest = [row for row in rows if row.get("source") != DEFAULT_LANDING_LABEL]
    return landing + rest


def _to_summary(raw: dict[str, Any]) -> SourceSummaryObject:
    """Convert registry list row to ``SourceSummaryObject``."""
    return SourceSummaryObject(
        name=raw.get("source", ""),
        resource_type=raw.get("resource_type", "custom"),
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
        origin=raw.get("origin"),
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
