"""System router — deployment facts, settings summary, default retention.

GET /api/v1/system/deployment  → What this deployment IS: profile, transports, mesh, routing, versions
GET /api/v1/system/version     → What this deployment runs: stack, engine, schemas, ui
GET /api/v1/system/schema      → What the last schema bootstrap pass did, object by object
GET /api/v1/system/settings    → Redacted settings summary
GET /api/v1/system/retention   → The deployment default TTL
"""

from __future__ import annotations

import sys
from typing import Any, Literal

from dfe_schemas import __version__ as schemas_version
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from scalo.logger import logger

from dfe_engine import __version__
from dfe_engine.api.deps import (
    CurrentUser,
    Settings,
    require_action,
)
from dfe_engine.appmgmt import DeployTarget, appconfig, routing
from dfe_engine.auth.rbac_scopes import scopes_dict
from dfe_engine.gitcrud import GitCrud
from dfe_engine.gitops.pins import UI_COMPONENT, component_overrides, load_pins, stack_version

router = APIRouter(prefix="/system", tags=["System"])


# ── Deploy-repo access ───────────────────────────────────────


def _optional_gitcrud(request: Request) -> GitCrud | None:
    """The deploy repo, or None when gitops is disabled. For readers with a default."""
    return getattr(request.app.state, "gitcrud", None)


# ── Response models ──────────────────────────────────────────


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


class SettingsSummary(BaseModel):
    """Redacted settings — no secrets."""

    clickhouse_host: str
    clickhouse_database: str
    clickhouse_data_database: str
    clickhouse_default_ttl_days: int = Field(
        description="Retention in days a time-series table gets when it declares none; 0 = none."
    )
    sources_dir: str
    services_config_dir: str
    hunt_dir: str
    auth_enabled: bool
    auth_local_enabled: bool
    api_host: str
    api_port: int
    api_cors_origins: list[str]


# ── Endpoints ────────────────────────────────────────────────


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
    "/settings",
    response_model=SettingsSummary,
    dependencies=[Depends(require_action(scopes_dict["system_read"]))],
)
async def get_settings(user: CurrentUser, settings: Settings):
    """Get a redacted summary of current settings. No secrets exposed."""
    return SettingsSummary(
        clickhouse_host=settings.clickhouse.host,
        clickhouse_database=settings.clickhouse.database,
        clickhouse_data_database=settings.clickhouse.effective_data_database,
        clickhouse_default_ttl_days=settings.clickhouse.default_ttl_days,
        sources_dir=settings.source.sources_dir,
        services_config_dir=settings.services.config_yaml_dir,
        hunt_dir=settings.hunts.hunt_dir,
        auth_enabled=settings.auth.enabled,
        auth_local_enabled=settings.auth.local.enabled,
        api_host=settings.api.host,
        api_port=settings.api.port,
        api_cors_origins=settings.api.cors_origins,
    )


# ── Default retention ────────────────────────────────────────


class RetentionStatus(BaseModel):
    """The deployment default TTL, as the environment sets it."""

    default_ttl_days: int = Field(
        description=(
            "Retention in days a time-series table gets when it declares none; 0 = no TTL. "
            "Set by DFE_CLICKHOUSE_DEFAULT_TTL_DAYS and applied to every table on engine start."
        )
    )


@router.get(
    "/retention",
    response_model=RetentionStatus,
    dependencies=[Depends(require_action(scopes_dict["system_read"]))],
)
async def get_retention(user: CurrentUser, settings: Settings) -> RetentionStatus:
    """The deployment default TTL."""
    return RetentionStatus(default_ttl_days=settings.clickhouse.default_ttl_days)


# ── ClickHouse Cloud lifecycle (control plane) ───────────────


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
