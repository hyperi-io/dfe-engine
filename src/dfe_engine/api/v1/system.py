"""System router -- deployment facts, settings summary, default retention.

GET /api/v1/system/deployment  -> What this deployment IS: profile, transports, mesh, routing, versions
GET /api/v1/system/version     -> What this deployment runs: stack, engine, schemas, ui
GET /api/v1/system/schema      -> What the last schema bootstrap pass did, object by object
GET /api/v1/system/status      -> Conditions that leave the engine degraded while it serves
GET /api/v1/system/settings    -> Redacted settings summary
GET /api/v1/system/retention   -> The effective default TTL and where it comes from
PUT /api/v1/system/retention   -> Set or clear the admin's override, then apply it
GET /api/v1/system/defaults    -> TTL, common header and merge engine a source inherits
PATCH /api/v1/system/defaults  -> Change any of those; only ttl_days is applied live
POST /api/v1/system/defaults/apply -> Pin those defaults onto the named sources and their tables
GET /api/v1/system/defaults/drift  -> Sources and tables off the defaults, searched and paginated
"""

import sys
from typing import Any, Literal

from dfe_schemas import __version__ as schemas_version
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from scalo.logger import logger

from dfe_engine import __version__
from dfe_engine.api.deps import (
    CurrentUser,
    Settings,
    SourceReg,
    get_clickhouse_client,
    get_interactive_clickhouse_client,
    require_action,
)
from dfe_engine.api.errors import ErrorResponse
from dfe_engine.api.pagination import PaginatedResponse, PaginationParams, apply_search
from dfe_engine.api.write_turn import WRITE_TURN
from dfe_engine.appmgmt import DeployTarget, appconfig, routing
from dfe_engine.auth.audit import audit_resource_change
from dfe_engine.auth.rbac_scopes import scopes_dict
from dfe_engine.git_identity import git_author
from dfe_engine.gitcrud import GitCrud
from dfe_engine.gitcrud.retention import (
    MAX_DEFAULT_TTL_DAYS,
    effective_settings,
    resolve_state,
    set_stored,
)
from dfe_engine.gitcrud.table_defaults import (
    UNSET,
    SourceDrift,
    commit_patch,
    pin_table_defaults,
    source_default_drift,
)
from dfe_engine.gitcrud.table_defaults import resolve as resolve_defaults
from dfe_engine.gitops.pins import UI_COMPONENT, component_overrides, load_pins, stack_version
from dfe_engine.schema.applier import LiveTable, SchemaApplyError, live_tables
from dfe_engine.schema.retention import (
    LiveStatus,
    SourceLiveOutcome,
    apply_pinned_defaults,
    reconcile_default_ttl,
)
from dfe_engine.source.models import Source
from dfe_engine.source.registry import SourceCoreResourceError, SourceNotFoundError
from dfe_engine.yaml_health import write_health

router = APIRouter(prefix="/system", tags=["System"])


# -- Deploy-repo access ---------------------------------------


def _optional_gitcrud(request: Request) -> GitCrud | None:
    """The deploy repo, or None when gitops is disabled. For readers with a default."""
    return getattr(request.app.state, "gitcrud", None)


def _gitcrud(request: Request) -> GitCrud:
    """The deploy repo, or 503. For writers, which have nowhere else to commit."""
    gc = _optional_gitcrud(request)
    if gc is None:
        raise HTTPException(
            status_code=503,
            detail={"code": "not_configured", "message": "gitops is not enabled"},
        )
    return gc


# -- Response models ------------------------------------------


class VersionResponse(BaseModel):
    """What this deployment runs: the certified stack, and the parts of it."""

    stack: str | None = Field(
        description="Certified stack version this deployment runs; null when nothing states one."
    )
    engine: str = Field(description="dfe-engine package version")
    schemas: str = Field(
        description=(
            "dfe-schemas release the engine applies its schema from. The wheel rides "
            "inside the engine image, so this is the schema version this deployment is on."
        )
    )
    ui: str | None = Field(
        description="dfe-ui version when the deploy repo pins one off the certified stack."
    )
    apps: dict[str, str] = Field(
        description=(
            "Every component the deploy repo pins off the certified stack, name to "
            "version tag; empty when the deploy repo carries no pins. Includes "
            "dfe-ui, which then matches the ui field above."
        )
    )
    source: Literal["deploy-repo", "deployment", "engine"] = Field(
        description=(
            "deploy-repo when the stack version came from pins.yaml, deployment when it "
            "came from what deployed this pod, else engine."
        )
    )
    python_version: str = Field(description="Python interpreter version")


class SchemaObjectStatus(BaseModel):
    """What the last pass did to one manifest object."""

    id: str = Field(description="The object's manifest id, e.g. data.main")
    kind: str = Field(description="database, table, materialized_view, view, role or topic")
    object: str = Field(description="Database-qualified name, or topic:<name>")
    action: str = Field(description="created, altered, unchanged, refused or skipped")
    checksum: str = Field(description="sha256 of the normalised rendered statement")
    columns_added: list[str] = Field(default_factory=list)
    drift: list[str] = Field(
        default_factory=list,
        description="Non-additive differences found; each one is why the change was refused.",
    )
    extra_columns: list[str] = Field(
        default_factory=list,
        description="Live columns the schema no longer declares. Reported, never actioned.",
    )


class SchemaStatusResponse(BaseModel):
    """The schema bootstrap phase's record of its last pass."""

    state: str = Field(description="unknown, running, converged, observed or failed")
    ready: bool = Field(description="Whether readiness may be reported on the schema check.")
    converged: bool = Field(
        description="Whether the manifest's objects are known to exist on this deployment."
    )
    schemas_version: str = Field(description="dfe-schemas release the plan was rendered from")
    engine_version: str = Field(description="dfe-engine release that ran the pass")
    topology: str = Field(description="single, replicated or replicated_on_cluster")
    database: str = Field(description="The one database every DFE object lands in")
    holder: str = Field(description="Who held the bootstrap lease for this pass")
    started_at: str
    finished_at: str
    duration_seconds: float
    error: str = Field(description="Why the pass failed; empty when it did not.")
    counts: dict[str, int] = Field(default_factory=dict, description="One count per action")
    objects: list[SchemaObjectStatus] = Field(default_factory=list)
    refused: list[str] = Field(
        default_factory=list,
        description="Objects whose change was declined as drift, each named with the reason.",
    )
    overlay_refused: list[str] = Field(
        default_factory=list,
        description="Overlay objects refused for redefining a core path.",
    )
    topics_created: list[str] = Field(default_factory=list)
    topics_skipped: str = Field(
        description="Why the topic set was skipped; empty when it was applied."
    )
    topics_drift: list[str] = Field(
        default_factory=list,
        description=(
            "Existing topics this pass could not confirm match the manifest, each named "
            "with what was found -- a shape difference, or that the shape was unreadable "
            "and so drift is unknown. A broker that answers a topic list but not a "
            "describe puts every existing topic here. Reported, never applied: the engine "
            "leaves an existing topic alone rather than altering a live broker on a deploy."
        ),
    )


class TransportFacts(BaseModel):
    """What this deployment can carry a source's records on, between its stages."""

    default: str = Field(description="Transport a source that names none takes here.")
    available: list[str] = Field(
        description=(
            "Every transport a source may name here. One entry: a deployment binds "
            "its stages to a single transport, and a source on the other is refused "
            "at save."
        )
    )


class MeshFacts(BaseModel):
    """Whether this deployment's stage pools sit behind balancing listeners."""

    enabled: bool = Field(description="True where a listener fronts each stage pool.")
    namespace: str = Field(
        description="Namespace holding those listeners; empty when the pools are dialled directly."
    )


class DeploymentResponse(BaseModel):
    """What this deployment IS, so a console never has to guess at it."""

    profile: str = Field(
        description=(
            "The tier this deployment was stood up as (DFE_PROFILE). Empty means "
            "the deployer named none, and every optional app is then reported as "
            "offered rather than hidden."
        )
    )
    transports: TransportFacts
    mesh: MeshFacts
    applies_routing: bool = Field(
        description=(
            "Whether the routing a deployed source compiles to reaches the apps "
            "that run it. A GitOps controller applies it on Kubernetes; off it the "
            "engine renders each app's config file into a directory the containers "
            "mount. False where neither is wired, so a console must not offer the "
            "source as live."
        )
    )
    stack: str | None = Field(
        description="Certified stack version this deployment runs; null when nothing states one."
    )
    engine: str = Field(description="dfe-engine package version")
    ui: str | None = Field(
        description=(
            "dfe-ui version, from the deploy repo's pins where they name one, else "
            "the version the chart was rendered with. Null when neither states one."
        )
    )
    apps: dict[str, str] = Field(
        description=(
            "Every component the deploy repo pins off the certified stack, name to "
            "version tag; empty when the deploy repo carries no pins. Includes "
            "dfe-ui, which then matches the ui field above."
        )
    )
    source: Literal["deploy-repo", "deployment", "engine"] = Field(
        description=(
            "deploy-repo when the stack version came from pins.yaml, deployment when it "
            "came from what deployed this pod, else engine."
        )
    )


class DegradedCondition(BaseModel):
    """One condition the engine keeps serving through, with the old content in place."""

    kind: Literal["yaml_write"] = Field(
        description="yaml_write: a write was refused and its target kept the content it had."
    )
    target: str = Field(
        description="The file path, or deploy-repo:<path> for a file in the deploy repo."
    )
    reason: Literal["dump", "verify"] = Field(
        description=(
            "dump: turning the data into YAML failed. verify: the YAML produced does "
            "not read back as the data."
        )
    )
    error: str = Field(description="The exception type behind the refusal.")
    since: str = Field(
        description="RFC 3339 time of the first refusal since the target last wrote cleanly."
    )


class SystemStatusResponse(BaseModel):
    """Whether the engine is serving degraded, and why."""

    status: Literal["ok", "degraded"] = Field(
        description="degraded while any condition is listed. Readiness does not follow it."
    )
    degraded: list[DegradedCondition] = Field(
        default_factory=list,
        description="Each condition, cleared when its target next writes cleanly.",
    )


class SettingsSummary(BaseModel):
    """Redacted settings -- no secrets."""

    clickhouse_host: str
    clickhouse_database: str
    clickhouse_data_database: str
    clickhouse_default_ttl_days: int = Field(
        description=(
            "Retention in days a time-series table gets when it declares none; 0 = none. "
            "The admin's override when one is set, else DFE_CLICKHOUSE_DEFAULT_TTL_DAYS."
        )
    )
    sources_dir: str
    services_config_dir: str
    hunt_dir: str
    auth_enabled: bool
    auth_local_enabled: bool
    api_host: str
    api_port: int
    api_cors_origins: list[str]


# -- Endpoints ------------------------------------------------


def deployment_facts(request: Request, settings: Any) -> DeploymentResponse:
    """Everything this deployment can state about itself, read once.

    Both /system/deployment and /system/version answer from here, so the versions
    the console footer shows and the versions its deployment card shows cannot
    disagree.

    The pins win where there are any: they are what the operator chose. A deploy
    with no pins base still knows what stood it up, because the chart passes that
    in, so the footer shows a stack version rather than nothing. With neither, the
    engine's own version is the whole answer.
    """
    gc = _optional_gitcrud(request)
    if gc is None:
        pins = {}
    else:
        # pins.yaml is read straight off the working tree, so it takes the same
        # guard every gitcrud read takes rather than racing a publish's reset.
        with gc.reading():
            pins = load_pins(gc.repo_path)
    pinned = stack_version(pins)
    stack = pinned or settings.stack_version or None
    apps = component_overrides(pins)
    transport = settings.transport
    return DeploymentResponse(
        profile=settings.deployment.profile,
        transports=TransportFacts(
            default=transport.default,
            available=sorted(transport.available()),
        ),
        mesh=MeshFacts(
            enabled=transport.mesh_enabled,
            namespace=transport.mesh_namespace,
        ),
        applies_routing=routing.reaches_apps(
            DeployTarget(settings.deployment.target),
            writes_app_config=appconfig.enabled(settings),
        ),
        stack=stack,
        engine=__version__,
        ui=apps.get(UI_COMPONENT) or settings.ui_version or None,
        apps=apps,
        source="deploy-repo" if pinned else ("deployment" if stack else "engine"),
    )


@router.get("/deployment", response_model=DeploymentResponse)
async def get_deployment(
    user: CurrentUser, request: Request, settings: Settings
) -> DeploymentResponse:
    """What this deployment IS: its profile, what it can carry records on, and its versions.

    Authenticated but ungated, for the same reason /version is: every console pane
    that must not guess reads this, and the body carries deployment shape and
    versions only. Without it the console has to assume the widest deployment and
    offer a transport or an app this tier does not run.
    """
    return deployment_facts(request, settings)


@router.get("/version", response_model=VersionResponse)
async def get_version(user: CurrentUser, request: Request, settings: Settings) -> VersionResponse:
    """What this deployment runs.

    Authenticated but ungated on purpose: the console footer is on every page, and
    the body carries versions only.
    """
    facts = deployment_facts(request, settings)
    return VersionResponse(
        stack=facts.stack,
        engine=facts.engine,
        schemas=schemas_version,
        ui=facts.ui,
        apps=facts.apps,
        source=facts.source,
        python_version=sys.version.split()[0],
    )


@router.get("/schema", response_model=SchemaStatusResponse)
async def get_schema_status(user: CurrentUser, settings: Settings) -> SchemaStatusResponse:
    """What the last schema bootstrap pass did, object by object.

    This is the operator's record of the schema apply, replacing the completed
    ArgoCD Job the engine took over from. ``state`` is what readiness follows:
    converged or observed is ready, failed leaves the pod up and NotReady with
    the cause here.
    """
    from dfe_engine.schema.phase import current_state

    return SchemaStatusResponse.model_validate(current_state().as_dict())


@router.get(
    "/status",
    response_model=SystemStatusResponse,
    dependencies=[Depends(require_action(scopes_dict["system_read"]))],
)
async def get_system_status(user: CurrentUser) -> SystemStatusResponse:
    """Conditions that leave the engine degraded while it keeps serving.

    A refused YAML write keeps the old file or the old deploy-repo content, so reads
    stay correct and readiness is not failed; this is where the refusal shows.
    """
    degraded = [
        DegradedCondition(
            kind="yaml_write",
            target=entry.target,
            reason=entry.reason,
            error=entry.error,
            since=entry.since,
        )
        for entry in write_health().degraded()
    ]
    return SystemStatusResponse(status="degraded" if degraded else "ok", degraded=degraded)


@router.get(
    "/settings",
    response_model=SettingsSummary,
    dependencies=[Depends(require_action(scopes_dict["system_read"]))],
)
def get_settings(user: CurrentUser, request: Request, settings: Settings):
    """Get a redacted summary of current settings. No secrets exposed."""
    return SettingsSummary(
        clickhouse_host=settings.clickhouse.host,
        clickhouse_database=settings.clickhouse.database,
        clickhouse_data_database=settings.clickhouse.effective_data_database,
        clickhouse_default_ttl_days=resolve_state(_optional_gitcrud(request), settings).effective,
        sources_dir=settings.source.sources_dir,
        services_config_dir=settings.services.config_yaml_dir,
        hunt_dir=settings.hunts.hunt_dir,
        auth_enabled=settings.auth.enabled,
        auth_local_enabled=settings.auth.local.enabled,
        api_host=settings.api.host,
        api_port=settings.api.port,
        api_cors_origins=settings.api.cors_origins,
    )


# -- Default retention ----------------------------------------


class RetentionStatus(BaseModel):
    """The default TTL a time-series table gets when it declares none, and where it comes from."""

    default_ttl_days: int = Field(
        description=(
            "Effective retention in days for every table that follows the default; "
            "0 = no TTL. The override when one is stored, else deployment_default."
        )
    )
    stored: int | None = Field(
        description="The admin's override, committed in the deploy repo; null when none is set."
    )
    origin: Literal["override", "deployment"] = Field(
        description="override when the stored value wins, deployment when deployment_default does."
    )
    deployment_default: int = Field(
        description="DFE_CLICKHOUSE_DEFAULT_TTL_DAYS as deployed; applies whenever no override is set."
    )
    editable: bool = Field(
        description=(
            "Whether this deployment can store an override. False without gitops, "
            "where a PUT answers 503."
        )
    )


class RetentionUpdate(BaseModel):
    """An admin's change to the default TTL."""

    default_ttl_days: int | None = Field(
        strict=True,
        ge=0,
        le=MAX_DEFAULT_TTL_DAYS,
        description=(
            "Retention in days for every table that follows the default; 0 keeps rows "
            "forever. null clears the override, so deployment_default applies again."
        ),
    )


class RetentionReconcileSummary(BaseModel):
    """What applying the new default did to the live tables."""

    core_tables_altered: list[str] = Field(
        description="database.table: <old> -> <new> days, for each engine table whose TTL moved."
    )
    source_tables_altered: list[str] = Field(
        description="database.table for each deployed source's table the apply altered."
    )
    sources_reconciled: int = Field(description="Deployed sources whose table was brought current.")
    sources_skipped: int = Field(
        description=(
            "Deployed sources left to their next deploy: table absent, build failed "
            "or ClickHouse refused."
        )
    )


class RetentionUpdateResponse(RetentionStatus):
    """The default TTL after the change, and what applying it did."""

    reconcile: RetentionReconcileSummary


def _retention_status(request: Request, settings: Any) -> RetentionStatus:
    gc = _optional_gitcrud(request)
    state = resolve_state(gc, settings)
    return RetentionStatus(
        default_ttl_days=state.effective,
        stored=state.stored,
        origin=state.origin,
        deployment_default=state.deployment_default,
        editable=gc is not None,
    )


@router.get(
    "/retention",
    response_model=RetentionStatus,
    dependencies=[Depends(require_action(scopes_dict["system_read"]))],
)
def get_retention(user: CurrentUser, request: Request, settings: Settings) -> RetentionStatus:
    """The effective default TTL, the admin's override and the deployment's own value."""
    return _retention_status(request, settings)


@router.put(
    "/retention",
    response_model=RetentionUpdateResponse,
    dependencies=[Depends(require_action(scopes_dict["system_write"])), WRITE_TURN],
    responses={
        502: {
            "model": ErrorResponse,
            "description": (
                "reconcile_failed: the value is stored, and applying it to ClickHouse "
                "failed; sending the same value again applies it"
            ),
        },
        503: {
            "model": ErrorResponse,
            "description": "not_configured: gitops is off, so there is nowhere to store the value",
        },
    },
)
def put_retention(
    body: RetentionUpdate,
    user: CurrentUser,
    request: Request,
    settings: Settings,
    sources: SourceReg,
) -> RetentionUpdateResponse:
    """Store the override in the deploy repo, then apply it to every table that follows it.

    The engine's own tables and every deployed source's table are brought to the new
    effective default in this request, with ``ALTER TABLE ... MODIFY TTL`` (or
    ``REMOVE TTL`` for 0). A table that declares its own TTL is left alone. A
    ClickHouse failure answers 502 with the override ALREADY committed; sending the
    same value again applies it.
    """
    gc = _gitcrud(request)
    set_stored(gc, body.default_ttl_days, user.user_id)
    audit_resource_change(
        user.user_id, "system", "retention", "updated", {"default_ttl_days": body.default_ttl_days}
    )
    status = _retention_status(request, settings)
    logger.info(
        "default TTL set via API",
        actor=user.user_id,
        days=status.default_ttl_days,
        origin=status.origin,
    )
    try:
        outcome = reconcile_default_ttl(
            get_clickhouse_client(settings),
            settings=effective_settings(settings, gc),
            sources=sources.get_all_sources(),
        )
    # The override is committed by now, so any ClickHouse failure has to say so rather than 500.
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail={
                "code": "reconcile_failed",
                "message": f"override stored; applying it to ClickHouse failed: {exc}",
            },
        ) from exc
    if outcome.sources_skipped:
        logger.warning(
            "default TTL: sources left to their next deploy", count=outcome.sources_skipped
        )
    return RetentionUpdateResponse(
        **status.model_dump(),
        reconcile=RetentionReconcileSummary(
            core_tables_altered=outcome.core_altered,
            source_tables_altered=[
                f"{t.database}.{t.table}" for t in outcome.report.tables if t.action == "altered"
            ],
            sources_reconciled=outcome.sources_reconciled,
            sources_skipped=outcome.sources_skipped,
        ),
    )


# -- Table defaults -------------------------------------------


class IntDefault(BaseModel):
    """One integer default: the value in force, and where it came from."""

    effective: int
    stored: int | None = Field(description="The admin's override; null when none is stored.")
    origin: Literal["override", "deployment"]
    deployment_default: int


class StrDefault(BaseModel):
    """One string default: the value in force, and where it came from."""

    effective: str
    stored: str | None = Field(description="The admin's override; null when none is stored.")
    origin: Literal["override", "deployment"]
    deployment_default: str


class SystemDefaults(BaseModel):
    """What a source inherits when it leaves TTL, header or engine unset."""

    ttl_days: IntDefault
    common_header_type: StrDefault
    common_header_version: StrDefault
    engine: StrDefault = Field(description="MergeTree-family variant, without a topology prefix.")
    editable: bool = Field(
        description="Whether this deployment can store an override. False without gitops."
    )


class SystemDefaultsPatch(BaseModel):
    """A partial change. Omitted fields stay as they are; null clears that override."""

    model_config = ConfigDict(extra="ignore")

    ttl_days: int | None = Field(
        default=None,
        strict=True,
        ge=0,
        le=MAX_DEFAULT_TTL_DAYS,
        description="Retention in days; 0 keeps rows forever. null clears the override.",
    )
    common_header_type: str | None = Field(
        default=None,
        description="Common-header profile name, such as timeseries or minimal. null clears it.",
    )
    common_header_version: str | None = Field(
        default=None,
        description="Common-header profile version. null clears it.",
    )
    engine: str | None = Field(
        default=None,
        description="MergeTree-family variant. null clears it, so the deployment engine applies.",
    )

    @field_validator("common_header_type", "common_header_version", "engine")
    @classmethod
    def _not_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped


class SystemDefaultsUpdate(SystemDefaults):
    """The defaults after the patch. reconcile is set only when ttl_days was named."""

    reconcile: RetentionReconcileSummary | None = None


def _system_defaults(request: Request, settings: Any) -> SystemDefaults:
    gc = _optional_gitcrud(request)
    ttl = resolve_state(gc, settings)
    table = resolve_defaults(gc, settings)
    return SystemDefaults(
        ttl_days=IntDefault(
            effective=ttl.effective,
            stored=ttl.stored,
            origin=ttl.origin,
            deployment_default=ttl.deployment_default,
        ),
        common_header_type=StrDefault(
            effective=table.header_type,
            stored=table.header_type_stored,
            origin=table.header_type_origin,
            deployment_default=table.header_type_fallback,
        ),
        common_header_version=StrDefault(
            effective=table.header_version,
            stored=table.header_version_stored,
            origin=table.header_version_origin,
            deployment_default=table.header_version_fallback,
        ),
        engine=StrDefault(
            effective=table.engine,
            stored=table.engine_stored,
            origin=table.engine_origin,
            deployment_default=table.engine_fallback,
        ),
        editable=gc is not None,
    )


def _reconcile_summary(outcome: Any) -> RetentionReconcileSummary:
    return RetentionReconcileSummary(
        core_tables_altered=outcome.core_altered,
        source_tables_altered=[
            f"{t.database}.{t.table}" for t in outcome.report.tables if t.action == "altered"
        ],
        sources_reconciled=outcome.sources_reconciled,
        sources_skipped=outcome.sources_skipped,
    )


@router.get(
    "/defaults",
    response_model=SystemDefaults,
    dependencies=[Depends(require_action(scopes_dict["system_read"]))],
)
def get_defaults(user: CurrentUser, request: Request, settings: Settings) -> SystemDefaults:
    """The TTL, common header and merge engine a source inherits when it sets none."""
    return _system_defaults(request, settings)


@router.patch(
    "/defaults",
    response_model=SystemDefaultsUpdate,
    dependencies=[Depends(require_action(scopes_dict["system_write"])), WRITE_TURN],
    responses={
        422: {
            "model": ErrorResponse,
            "description": "validation_error: a named value cannot be stored",
        },
        502: {
            "model": ErrorResponse,
            "description": (
                "reconcile_failed: ttl_days is stored, and applying it to ClickHouse failed; "
                "sending the same value again applies it"
            ),
        },
        503: {
            "model": ErrorResponse,
            "description": "not_configured: gitops is off, so there is nowhere to store the value",
        },
    },
)
def patch_defaults(
    body: SystemDefaultsPatch,
    user: CurrentUser,
    request: Request,
    settings: Settings,
    sources: SourceReg,
) -> SystemDefaultsUpdate:
    """Store the named defaults. Only ttl_days is applied to live tables.

    Omitted fields are left alone. null clears that override. Common-header type,
    version and engine are what a source inherits the next time it is deployed;
    this request does not rewrite them onto live tables. A patch that names
    ttl_days stores it and then runs the same reconcile as PUT /retention.
    """
    gc = _gitcrud(request)
    named = body.model_fields_set
    try:
        commit_patch(
            gc,
            actor=user.user_id,
            settings=settings,
            ttl_days=body.ttl_days if "ttl_days" in named else UNSET,
            common_header_type=body.common_header_type if "common_header_type" in named else UNSET,
            common_header_version=(
                body.common_header_version if "common_header_version" in named else UNSET
            ),
            engine=body.engine if "engine" in named else UNSET,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "validation_error", "message": str(exc)},
        ) from exc
    if named:
        audit_resource_change(
            user.user_id,
            "system",
            "defaults",
            "updated",
            {key: getattr(body, key) for key in sorted(named)},
        )
    status = _system_defaults(request, settings)
    logger.info(
        "table defaults set via API",
        actor=user.user_id,
        fields=sorted(named),
        ttl_days=status.ttl_days.effective,
        common_header_type=status.common_header_type.effective,
        engine=status.engine.effective,
    )
    reconcile = None
    if "ttl_days" in named:
        try:
            outcome = reconcile_default_ttl(
                get_clickhouse_client(settings),
                settings=effective_settings(settings, gc),
                sources=sources.get_all_sources(),
            )
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail={
                    "code": "reconcile_failed",
                    "message": f"override stored; applying it to ClickHouse failed: {exc}",
                },
            ) from exc
        if outcome.sources_skipped:
            logger.warning(
                "default TTL: sources left to their next deploy", count=outcome.sources_skipped
            )
        reconcile = _reconcile_summary(outcome)
    return SystemDefaultsUpdate(**status.model_dump(), reconcile=reconcile)


class ApplyDefaultsRequest(BaseModel):
    """The sources to pin to the current table defaults."""

    sources: list[str] = Field(min_length=1, description="Source names. Only these are written.")

    @field_validator("sources")
    @classmethod
    def _names(cls, value: list[str]) -> list[str]:
        names: list[str] = []
        for name in value:
            stripped = name.strip()
            if not stripped:
                raise ValueError("source id must not be blank")
            if stripped not in names:
                names.append(stripped)
        return names


class FieldNotApplied(BaseModel):
    """A pinned value the deployed table did not take."""

    field: Literal["ttl_days", "common_header_type", "common_header_version", "engine"]
    reason: str


class SourceLiveApply(BaseModel):
    """What applying the defaults did to one named source's deployed table."""

    source: str
    status: LiveStatus = Field(
        description=(
            "altered or unchanged once the table was brought to the pinned values; "
            "not_deployed when no table runs the pinned version; failed when "
            "ClickHouse or the schema build refused"
        )
    )
    table: str | None = Field(
        description="database.table; null when the source has never been deployed."
    )
    ttl: str | None = Field(
        description="The TTL move in days, such as '90 -> 91'; null when it did not move."
    )
    columns_added: list[str] = Field(
        description=(
            "Columns the pinned version declares that the table lacked: the pinned "
            "header's, and any other the version's schema adds."
        )
    )
    not_applied: list[FieldNotApplied] = Field(
        description="Pinned values the table did not take, each with the reason."
    )
    reason: str | None = Field(
        description="Why nothing reached the table, for not_deployed and failed; else null."
    )


def _live_body(outcome: SourceLiveOutcome) -> SourceLiveApply:
    return SourceLiveApply(
        source=outcome.source,
        status=outcome.status,
        table=outcome.table or None,
        ttl=outcome.ttl or None,
        columns_added=list(outcome.columns_added),
        not_applied=[
            FieldNotApplied.model_validate({"field": item.field, "reason": item.reason})
            for item in outcome.not_applied
        ],
        reason=outcome.reason or None,
    )


class ApplyDefaultsResponse(BaseModel):
    """Which named sources were pinned, which already had these defaults, and their tables."""

    updated: list[str]
    unchanged: list[str]
    live: list[SourceLiveApply] = Field(
        description="One entry per named source, in request order: what reached its table."
    )


@router.post(
    "/defaults/apply",
    response_model=ApplyDefaultsResponse,
    dependencies=[Depends(require_action(scopes_dict["source_write"])), WRITE_TURN],
    responses={
        404: {"model": ErrorResponse, "description": "not_found: a named source does not exist"},
        409: {
            "model": ErrorResponse,
            "description": "conflict: a named source is engine-owned and cannot be edited",
        },
    },
)
def apply_defaults(
    body: ApplyDefaultsRequest,
    user: CurrentUser,
    request: Request,
    settings: Settings,
    registry: SourceReg,
) -> ApplyDefaultsResponse:
    """Pin the current TTL, common header and merge engine onto the named sources and their tables.

    Sources that are not in the list are left alone. A source that already stores
    these values is unchanged in the deploy repo. A missing or engine-owned name
    fails the request before any source is written.

    Then every named source whose current version is the deployed one, pinned now
    or earlier, has its table brought to the pinned values in this request: the
    TTL moves and the header's missing columns are added. The engine of an
    existing table needs a rebuild, so it reaches new tables only, and existing
    columns keep their type. ``live`` reports each source: what reached the table,
    what did not and why. A source with no table for the pinned version takes the
    values on its next deploy. A ClickHouse failure is reported on that source,
    and the pin stays committed.
    """
    gc = _optional_gitcrud(request)
    ttl = resolve_state(gc, settings)
    table = resolve_defaults(gc, settings)
    loaded: list[Any] = []
    for name in body.sources:
        try:
            source = registry.get_source(name)
        except SourceNotFoundError:
            raise HTTPException(
                status_code=404,
                detail={"code": "not_found", "message": f"Source {name!r} not found"},
            ) from None
        if registry.is_core(name):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "conflict",
                    "message": str(SourceCoreResourceError(action="modify", source=name)),
                    "source": name,
                },
            ) from None
        loaded.append(source)

    updated: list[str] = []
    unchanged: list[str] = []
    saved: list[Source] = []
    author = git_author(user)
    for source in loaded:
        pinned = pin_table_defaults(
            source,
            header_type=table.header_type,
            header_version=table.header_version,
            ttl_days=ttl.effective,
            engine=table.engine,
        )
        if pinned is None:
            unchanged.append(source.source)
            saved.append(source)
            continue
        stored = registry.save_source(
            pinned,
            created_by=author,
            description=f"source: apply table defaults to {source.source}",
        )
        saved.append(stored)
        audit_resource_change(
            user.user_id,
            "source",
            source.source,
            "updated",
            {
                "ttl_days": ttl.effective,
                "common_header_type": table.header_type,
                "common_header_version": table.header_version,
                "engine": table.engine,
            },
        )
        updated.append(source.source)

    def connect() -> Any:
        return get_clickhouse_client(settings)

    outcomes = apply_pinned_defaults(
        connect, settings=effective_settings(settings, gc), sources=saved
    )
    for outcome in outcomes:
        if outcome.status == "altered":
            audit_resource_change(
                user.user_id,
                "schema",
                outcome.source,
                "updated",
                {
                    "table": outcome.table,
                    "ttl": outcome.ttl,
                    "columns_added": list(outcome.columns_added),
                },
            )
    return ApplyDefaultsResponse(
        updated=updated, unchanged=unchanged, live=[_live_body(o) for o in outcomes]
    )


class DefaultComparison(BaseModel):
    """A stored source value, and its table's value, beside the default it is measured against."""

    stored: str | int | None
    default: str | int | None
    live: str | int | None = Field(
        default=None,
        description=(
            "The deployed table's value: TTL in days (0 for none) or the engine variant. "
            "null for the header, and when the source has no deployed table."
        ),
    )


class SourceDefaultDrift(BaseModel):
    """One source whose deployed table or stored values differ from the defaults."""

    source: str
    core: bool
    drifted: list[str]
    ttl_days: DefaultComparison
    common_header_type: DefaultComparison
    common_header_version: DefaultComparison
    engine: DefaultComparison


def _drift_body(report: SourceDrift) -> SourceDefaultDrift:
    def comparison(name: str) -> DefaultComparison:
        field = getattr(report, name)
        return DefaultComparison(stored=field.stored, default=field.default, live=field.live)

    return SourceDefaultDrift(
        source=report.source,
        core=report.core,
        drifted=list(report.drifted),
        ttl_days=comparison("ttl_days"),
        common_header_type=comparison("common_header_type"),
        common_header_version=comparison("common_header_version"),
        engine=comparison("engine"),
    )


def _drift_search_row(item: SourceDefaultDrift) -> dict[str, Any]:
    stored: list[str] = []
    for name in item.drifted:
        field = getattr(item, name)
        stored.extend(str(value) for value in (field.stored, field.live) if value is not None)
    return {
        "item": item,
        "source": item.source,
        "drifted": " ".join(item.drifted),
        "stored": " ".join(stored),
    }


def _deployed_tables(settings: Any, sources: list[Source]) -> dict[str, LiveTable]:
    """The data database's tables, read once, when any source has been deployed; else none.

    Read within ``clickhouse.interactive_budget_seconds``, so a down server answers
    503 in seconds.
    """
    if not any(source.deployed_version is not None for source in sources):
        return {}
    database = settings.clickhouse.effective_data_database
    try:
        return live_tables(get_interactive_clickhouse_client(settings), database)
    except SchemaApplyError as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "clickhouse_unavailable",
                "message": f"the deployed tables could not be read: {exc}",
            },
        ) from exc


@router.get(
    "/defaults/drift",
    response_model=PaginatedResponse[SourceDefaultDrift],
    dependencies=[Depends(require_action(scopes_dict["system_read"]))],
    responses={
        503: {
            "model": ErrorResponse,
            "description": (
                "clickhouse_unavailable: a source is deployed and its table could not be read"
            ),
        },
    },
)
def get_default_drift(
    user: CurrentUser,
    request: Request,
    settings: Settings,
    registry: SourceReg,
    pagination: PaginationParams = Depends(),
    search: str | None = Query(
        None,
        description="Search in source name, drifted field, and stored or live value",
    ),
) -> PaginatedResponse[SourceDefaultDrift]:
    """Sources whose table or current version runs a header, TTL or engine other than the default.

    TTL and engine are measured on a deployed source's table, read from ClickHouse,
    so a value pinned in the source that the table never took is still drift. An
    engine change persists as drift until the table is rebuilt. For a source with
    no deployed table, and for the header, the stored value is measured: a field
    the source leaves unset inherits the default on its next deploy and is not
    drift, nor is a stored value equal to the default. Header types compare as
    bare profile names, so ``common-header/timeseries`` is ``timeseries``.

    ``core`` is true for an engine-owned source, which the apply endpoint will
    refuse. The landing source is never listed. ``search`` matches the source
    name, a drifted field name, or a stored or live value. ``per_page=-1``
    returns every match.
    """
    gc = _optional_gitcrud(request)
    ttl = resolve_state(gc, settings)
    table = resolve_defaults(gc, settings)
    landing = settings.clickhouse.landing_table
    sources = [source for source in registry.get_all_sources() if source.source != landing]
    tables = _deployed_tables(settings, sources)
    found: list[SourceDefaultDrift] = []
    for source in sources:
        live = tables.get(source.table_name) if source.deployed_version is not None else None
        report = source_default_drift(
            source,
            header_type=table.header_type,
            header_version=table.header_version,
            ttl_days=ttl.effective,
            engine=table.engine,
            live=live,
        )
        if report is not None:
            found.append(_drift_body(report))
    found.sort(key=lambda item: item.source)
    rows = apply_search(
        [_drift_search_row(item) for item in found],
        search,
        ["source", "drifted", "stored"],
    )
    return PaginatedResponse.from_list(
        [row["item"] for row in rows],
        pagination.page,
        pagination.per_page,
    )


# -- ClickHouse Cloud lifecycle (control plane) ---------------


class CloudServiceStateResponse(BaseModel):
    """CH Cloud service control-plane state."""

    configured: bool = Field(description="Whether the CH Cloud control-plane key is set.")
    id: str = Field(default="", description="CH Cloud service id.")
    name: str = Field(default="", description="CH Cloud service name.")
    state: str = Field(
        default="", description="running / stopped / idle / starting / stopping / ..."
    )
    is_running: bool = Field(default=False, description="True when the service is running.")


def _cloud_state(settings) -> CloudServiceStateResponse:
    from dfe_engine.clickhouse.cloud import CloudService, CloudServiceError

    cloud = settings.clickhouse.cloud
    if not cloud.configured:
        return CloudServiceStateResponse(configured=False)
    try:
        st = CloudService(cloud).status()
    except CloudServiceError as exc:
        raise HTTPException(
            status_code=502, detail={"code": "cloud_error", "message": str(exc)}
        ) from exc
    return CloudServiceStateResponse(
        configured=True, id=st.id, name=st.name, state=st.state, is_running=st.is_running
    )


@router.get(
    "/clickhouse-cloud",
    response_model=CloudServiceStateResponse,
    dependencies=[Depends(require_action(scopes_dict["system_read"]))],
)
def clickhouse_cloud_status(user: CurrentUser, settings: Settings):
    """CH Cloud service control-plane status (read-only)."""
    return _cloud_state(settings)


@router.post(
    "/clickhouse-cloud/start",
    response_model=CloudServiceStateResponse,
    dependencies=[Depends(require_action(scopes_dict["clickhouse_cloud_manage"]))],
)
def clickhouse_cloud_start(user: CurrentUser, settings: Settings):
    """Start (wake) the CH Cloud service. BILLABLE + admin-gated + audited."""
    from dfe_engine.clickhouse.cloud import CloudService, CloudServiceError

    cloud = settings.clickhouse.cloud
    if not cloud.configured:
        raise HTTPException(
            status_code=503,
            detail={"code": "not_configured", "message": "ClickHouse Cloud is not configured"},
        )
    logger.info("CH Cloud start requested via API", actor=getattr(user, "username", "?"))
    try:
        st = CloudService(cloud).start()
    except CloudServiceError as exc:
        raise HTTPException(
            status_code=502, detail={"code": "cloud_error", "message": str(exc)}
        ) from exc
    return CloudServiceStateResponse(
        configured=True, id=st.id, name=st.name, state=st.state, is_running=st.is_running
    )


@router.post(
    "/clickhouse-cloud/stop",
    response_model=CloudServiceStateResponse,
    dependencies=[Depends(require_action(scopes_dict["clickhouse_cloud_manage"]))],
)
def clickhouse_cloud_stop(user: CurrentUser, settings: Settings):
    """Stop the CH Cloud service (saves cost). Admin-gated + audited."""
    from dfe_engine.clickhouse.cloud import CloudService, CloudServiceError

    cloud = settings.clickhouse.cloud
    if not cloud.configured:
        raise HTTPException(
            status_code=503,
            detail={"code": "not_configured", "message": "ClickHouse Cloud is not configured"},
        )
    logger.info("CH Cloud stop requested via API", actor=getattr(user, "username", "?"))
    try:
        st = CloudService(cloud).stop()
    except CloudServiceError as exc:
        raise HTTPException(
            status_code=502, detail={"code": "cloud_error", "message": str(exc)}
        ) from exc
    return CloudServiceStateResponse(
        configured=True, id=st.id, name=st.name, state=st.state, is_running=st.is_running
    )
