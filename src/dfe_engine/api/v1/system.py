"""System router — deployment facts, settings summary, default retention.

GET /api/v1/system/deployment  → What this deployment IS: profile, transports, mesh, routing, versions
GET /api/v1/system/version     → What this deployment runs: stack, engine, ui
GET /api/v1/system/settings    → Redacted settings summary
GET /api/v1/system/retention   → Effective default TTL and where it comes from
PUT /api/v1/system/retention   → Store the console override, reconcile the tables
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from scalo.logger import logger

from dfe_engine import __version__
from dfe_engine.api.deps import (
    CurrentUser,
    Settings,
    SourceReg,
    get_clickhouse_client,
    require_action,
)
from dfe_engine.appmgmt import DeployTarget, routing
from dfe_engine.auth.audit import audit_resource_change
from dfe_engine.auth.rbac_scopes import scopes_dict
from dfe_engine.gitcrud import GitCrud
from dfe_engine.gitcrud.retention import (
    RetentionState,
    deployment_days,
    resolve_state,
    set_stored,
)
from dfe_engine.gitops.pins import UI_COMPONENT, component_overrides, load_pins, stack_version
from dfe_engine.schema.applier import log_report
from dfe_engine.schema.retention import reconcile_default_ttl

router = APIRouter(prefix="/system", tags=["System"])


# ── Deploy-repo access ───────────────────────────────────────


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


# ── Response models ──────────────────────────────────────────


class VersionResponse(BaseModel):
    """What this deployment runs: the certified stack, and the parts of it."""

    stack: str | None = Field(
        description="Certified stack version this deployment runs; null when nothing states one."
    )
    engine: str = Field(description="dfe-engine package version")
    ui: str | None = Field(
        description="dfe-ui version when the deploy repo pins one off the certified stack."
    )
    source: Literal["deploy-repo", "deployment", "engine"] = Field(
        description=(
            "deploy-repo when the stack version came from pins.yaml, deployment when it "
            "came from what deployed this pod, else engine."
        )
    )
    python_version: str = Field(description="Python interpreter version")


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
            "that run it. False on a Compose stack, which mounts each app's config "
            "file read-only: the engine still writes the overlay, and nothing "
            "carries it into the container, so a console must not offer the source "
            "as live."
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
    pins = load_pins(gc.repo_path) if gc is not None else {}
    pinned = stack_version(pins)
    stack = pinned or settings.stack_version or None
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
        applies_routing=routing.reaches_apps(DeployTarget(settings.deployment.target)),
        stack=stack,
        engine=__version__,
        ui=component_overrides(pins).get(UI_COMPONENT) or settings.ui_version or None,
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
        ui=facts.ui,
        source=facts.source,
        python_version=sys.version.split()[0],
    )


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
    """The deployment default TTL: the override, the env value, and which one wins."""

    stored: int | None = Field(
        description="The console override committed in the deploy repo; null when none."
    )
    effective: int = Field(
        description="Retention in days a time-series table gets when it declares none; 0 = none."
    )
    origin: Literal["override", "deployment"] = Field(
        description="override when the stored value wins, deployment when the env default does."
    )
    deployment_default: int = Field(
        description="clickhouse.default_ttl_days as deployed (DFE_CLICKHOUSE_DEFAULT_TTL_DAYS)."
    )


class RetentionRequest(BaseModel):
    default_ttl_days: int | None = Field(
        ge=0, description="Override in days; 0 = no default TTL; null clears the override."
    )


class RetentionReconcileSummary(BaseModel):
    """What the reconcile that follows a PUT did to the live tables."""

    summary: str = Field(description="One line: databases created, tables created/altered/current.")
    tables_altered: list[str] = Field(
        description="database.table for every table whose TTL or columns changed."
    )
    sources_reconciled: int = Field(description="Deployed sources whose table was reconciled.")
    sources_skipped: int = Field(
        description="Deployed sources left to their next deploy (table absent or build failed)."
    )


class RetentionUpdateResponse(RetentionStatus):
    reconcile: RetentionReconcileSummary


def get_clickhouse_connector(settings: Settings) -> Callable[[], Any]:
    """Deferred client, so the override is committed before ClickHouse is touched.

    Tests override THIS dependency to inject a fake client.
    """
    return lambda: get_clickhouse_client(settings)


ClickHouseConnector = Annotated[Callable[[], Any], Depends(get_clickhouse_connector)]


def _retention_status(state: RetentionState, settings: Any) -> dict[str, Any]:
    return {
        "stored": state.stored,
        "effective": state.effective,
        "origin": state.origin,
        "deployment_default": deployment_days(settings),
    }


@router.get(
    "/retention",
    response_model=RetentionStatus,
    dependencies=[Depends(require_action(scopes_dict["system_read"]))],
)
async def get_retention(user: CurrentUser, request: Request, settings: Settings) -> RetentionStatus:
    """Effective default TTL and where it comes from. Answers without gitops too."""
    gc = _optional_gitcrud(request)
    if gc is None:
        state = RetentionState(
            stored=None, effective=deployment_days(settings), origin="deployment"
        )
    else:
        state = resolve_state(gc, settings)
    return RetentionStatus(**_retention_status(state, settings))


@router.put(
    "/retention",
    response_model=RetentionUpdateResponse,
    dependencies=[Depends(require_action(scopes_dict["system_write"]))],
)
async def put_retention(
    body: RetentionRequest,
    user: CurrentUser,
    request: Request,
    settings: Settings,
    sources: SourceReg,
    connect: ClickHouseConnector,
) -> RetentionUpdateResponse:
    """Store the override in the deploy repo, then reconcile every table that follows it.

    The core tables and every deployed source's table are brought to the new
    effective default in this request. A ClickHouse failure returns 502 with the
    override ALREADY committed: the next schema apply or source deploy picks it up.
    """
    gc = _gitcrud(request)
    set_stored(gc, body.default_ttl_days, user.user_id)
    audit_resource_change(
        user.user_id, "system", "retention", "updated", {"default_ttl_days": body.default_ttl_days}
    )
    state = resolve_state(gc, settings)
    logger.info(
        "default TTL set via API", actor=user.user_id, days=state.effective, origin=state.origin
    )
    try:
        outcome = reconcile_default_ttl(
            connect(), settings=settings, sources=sources.get_all_sources(), days=state.effective
        )
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail={
                "code": "reconcile_failed",
                "message": f"override stored; ClickHouse reconcile failed: {exc}",
            },
        ) from exc
    log_report(outcome.report, prefix="retention")
    if outcome.sources_skipped:
        logger.warning(
            "default TTL: sources left to their next deploy", count=outcome.sources_skipped
        )
    return RetentionUpdateResponse(
        **_retention_status(state, settings),
        reconcile=RetentionReconcileSummary(
            summary=outcome.report.summary(),
            tables_altered=[
                f"{t.database}.{t.table}" for t in outcome.report.tables if t.action == "altered"
            ],
            sources_reconciled=outcome.sources_reconciled,
            sources_skipped=outcome.sources_skipped,
        ),
    )


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
