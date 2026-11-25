"""System router -- deployment facts, settings summary, default retention.

GET /api/v1/system/deployment  -> What this deployment IS: profile, transports, mesh, routing,
                                  backing services, versions
GET /api/v1/system/version     -> What this deployment runs: stack, engine, schemas, ui
GET /api/v1/system/schema      -> What the last schema bootstrap pass did, object by object
GET /api/v1/system/status      -> Conditions that leave the engine degraded while it serves
GET /api/v1/system/settings    -> Redacted settings summary
GET /api/v1/system/retention   -> The effective default TTL and where it comes from
PUT /api/v1/system/retention   -> Set or clear the admin's override, then apply it
GET /api/v1/system/defaults    -> TTL, common header and merge engine a source inherits
PATCH /api/v1/system/defaults  -> Change any of those. Only ttl_days is applied live
POST /api/v1/system/defaults/apply -> Pin those defaults onto the named sources and their tables
GET /api/v1/system/defaults/drift  -> Sources and tables off the defaults, searched and paginated
"""

import functools
import sys
from typing import Any, Literal

from dfe_schemas import __version__ as schemas_version
from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from scalo.api import (
    DryRun,
    ListParams,
    Page,
    PreconditionHeaders,
    ProblemError,
    etag_of,
    paginate,
    scalo_extra,
)
from scalo.concurrency import run_blocking
from scalo.logger import logger

from dfe_engine import __version__
from dfe_engine.api.conventions import apply_search
from dfe_engine.api.deps import (
    CurrentUser,
    Settings,
    SourceReg,
    get_clickhouse_client,
    get_interactive_clickhouse_client,
    require_action,
)
from dfe_engine.api.errors import backend_failure, engine_message
from dfe_engine.api.review import (
    ReviewFields,
    review_fields,
    routing_audit_detail,
    set_review_headers,
)
from dfe_engine.api.v1.backing_services import BackingFacts, backing_facts
from dfe_engine.api.v1.orgs import reconcile_tenancy
from dfe_engine.api.write_turn import WRITE_TURN
from dfe_engine.appmgmt import DeployTarget, appconfig, routing
from dfe_engine.auth.audit import audit_org_change, audit_resource_change
from dfe_engine.auth.rbac_scopes import scopes_dict
from dfe_engine.git_identity import git_author
from dfe_engine.gitcrud import GitCrud
from dfe_engine.gitcrud.retention import (
    MAX_DEFAULT_TTL_DAYS,
    effective_settings,
    resolve_state,
    set_stored,
)
from dfe_engine.gitcrud.routing import ReviewRouting
from dfe_engine.gitcrud.table_defaults import (
    UNSET,
    SourceDrift,
    commit_patch,
    pin_table_defaults,
    source_default_drift,
)
from dfe_engine.gitcrud.table_defaults import resolve as resolve_defaults
from dfe_engine.gitops.pins import UI_COMPONENT, component_overrides, load_pins, stack_version
from dfe_engine.orgs.home import HomeOrgStore
from dfe_engine.schema.applier import LiveTable, SchemaApplyError, live_tables
from dfe_engine.schema.core_schema import landing_table_name
from dfe_engine.schema.retention import (
    LiveStatus,
    SourceLiveOutcome,
    apply_pinned_defaults,
    request_converge,
)
from dfe_engine.source.flow import runs_per_source_apps
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
        raise ProblemError(503, "gitops is not enabled", code="not_configured")
    return gc


def _cloud_error(exc: Exception) -> ProblemError:
    """The 502 for a ClickHouse Cloud management-API failure."""
    message = engine_message(
        exc,
        "the ClickHouse Cloud management API call failed; the engine log has the reason",
        event="ClickHouse Cloud management API call failed",
    )
    return ProblemError(502, message, code="cloud_error")


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
    source: Literal["deploy-repo", "registry", "deployment", "engine"] = Field(
        description=(
            "deploy-repo when the stack version came from pins.yaml, registry when the "
            "published stack manifests matched the images this deployment runs, deployment "
            "when it came from what deployed this pod, else engine."
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
    platform: str = Field(description="cloud or self_hosted, probed from the server")
    database: str = Field(description="The one database every DFE object lands in")
    holder: str = Field(description="Who held the bootstrap lease for this pass")
    started_at: str
    finished_at: str
    duration_seconds: float
    error: str = Field(
        description="The stage that failed, its cause in the engine log; empty when none did."
    )
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
    per_source_apps: bool = Field(
        description=(
            "Whether a source may need an app of its own (a fetcher or a transform). "
            "False on direct, which runs the receiver to loader path only, and such a "
            "source is refused at save."
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
            "The tier this deployment was stood up as (DFE_ENGINE_PROFILE). Empty means "
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
    source: Literal["deploy-repo", "registry", "deployment", "engine"] = Field(
        description=(
            "deploy-repo when the stack version came from pins.yaml, registry when the "
            "published stack manifests matched the images this deployment runs, deployment "
            "when it came from what deployed this pod, else engine."
        )
    )
    backing: dict[str, BackingFacts] = Field(
        description=(
            "Who runs each backing service, keyed as GET /backing-services names it. "
            "managed_by is dfe wherever the deployer states no provider."
        ),
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
            "The admin's override when one is set, else DFE_ENGINE_CLICKHOUSE_DEFAULT_TTL_DAYS."
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


def _stack_source(
    pinned: str | None, resolved: str | None, stack: str | None
) -> Literal["deploy-repo", "registry", "deployment", "engine"]:
    """Name where ``stack`` came from, in the order deployment_facts prefers them."""
    if pinned:
        return "deploy-repo"
    if resolved:
        return "registry"
    return "deployment" if stack else "engine"


def deployment_facts(request: Request, settings: Any) -> DeploymentResponse:
    """Everything this deployment can state about itself, read once.

    Both /system/deployment and /system/version answer from here, so the versions
    the console footer shows and the versions its deployment card shows cannot
    disagree.

    The pins win where there are any: they are what the operator chose. Next comes
    the published stack whose image digests match what this deployment runs, which
    the engine resolves in the background after startup. A deploy with neither still
    knows what stood it up, because the chart or compose passes that in, so the footer
    shows a stack version rather than nothing. With none of them, the engine's own
    version is the whole answer.

    Blocking: it reads the deploy repo, so a route calls it off the event loop.
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
    resolved = getattr(request.app.state, "resolved_stack", None)
    stack = pinned or resolved or settings.stack_version or None
    apps = component_overrides(pins)
    transport = settings.transport
    return DeploymentResponse(
        profile=settings.deployment.profile,
        transports=TransportFacts(
            default=transport.default,
            available=sorted(transport.available()),
            per_source_apps=runs_per_source_apps(transport.default),
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
        source=_stack_source(pinned, resolved, stack),
        backing=backing_facts(request),
    )


@router.get("/deployment", response_model=DeploymentResponse)
async def get_deployment(
    user: CurrentUser, request: Request, settings: Settings
) -> DeploymentResponse:
    """What this deployment IS: its profile, transports, backing services and versions.

    Authenticated but ungated, for the same reason /version is: every console pane
    that must not guess reads this, and the body carries deployment shape and
    versions only. Without it the console has to assume the widest deployment and
    offer a transport or an app this tier does not run.
    """
    return await run_blocking(functools.partial(deployment_facts, request, settings))


@router.get("/version", response_model=VersionResponse, openapi_extra=scalo_extra(verb="version"))
async def get_version(user: CurrentUser, request: Request, settings: Settings) -> VersionResponse:
    """What this deployment runs.

    Authenticated but ungated on purpose: the console footer is on every page, and
    the body carries versions only.
    """
    facts = await run_blocking(functools.partial(deployment_facts, request, settings))
    return VersionResponse(
        stack=facts.stack,
        engine=facts.engine,
        schemas=schemas_version,
        ui=facts.ui,
        apps=facts.apps,
        source=facts.source,
        python_version=sys.version.split()[0],
    )


@router.get(
    "/schema", response_model=SchemaStatusResponse, openapi_extra=scalo_extra(verb="schema")
)
async def get_schema_status(user: CurrentUser, settings: Settings) -> SchemaStatusResponse:
    """What the last schema bootstrap pass did, object by object.

    This is the operator's record of the schema apply, replacing the completed
    ArgoCD Job the engine took over from. ``state`` is what readiness follows:
    converged is ready, failed leaves the pod up and NotReady with the stage that
    failed here and the cause in the engine log, and observed -- another applier
    held the lease for the whole wait -- stays NotReady until a later pass converges.
    """
    from dfe_engine.schema.phase import current_state

    return SchemaStatusResponse.model_validate(current_state().as_dict())


@router.get(
    "/status",
    openapi_extra=scalo_extra(verb="status"),
    response_model=SystemStatusResponse,
    dependencies=[Depends(require_action(scopes_dict["system_read"]))],
)
async def get_system_status(user: CurrentUser) -> SystemStatusResponse:
    """Conditions that leave the engine degraded while it keeps serving.

    A refused YAML write keeps the old file or the old deploy-repo content, so reads
    stay correct and readiness is not failed. This is where the refusal shows.
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
    openapi_extra=scalo_extra(verb="settings"),
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
        description="DFE_ENGINE_CLICKHOUSE_DEFAULT_TTL_DAYS as deployed; applies whenever no override is set."
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
    response_model=RetentionStatus,
    dependencies=[Depends(require_action(scopes_dict["system_write"])), WRITE_TURN],
    responses={
        503: {
            "description": "not_configured: gitops is off, so there is nowhere to store the value",
        },
    },
)
def put_retention(
    body: RetentionUpdate,
    user: CurrentUser,
    request: Request,
    settings: Settings,
) -> RetentionStatus:
    """Store the override in the deploy repo and return, leaving the tables to a background pass.

    The pass brings the engine's own tables and every deployed source's table to the
    stored value with ``ALTER TABLE ... MODIFY TTL`` (or ``REMOVE TTL`` for 0). A table
    that declares its own TTL is left alone. The pass runs again until every table
    has it, so a ClickHouse that is down when the value is stored gets it once it is
    back, and a restart does not lose it.
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
    request_converge(request.app.state)
    return status


# -- Your organisation ----------------------------------------


class YourOrganisation(BaseModel):
    """The org the deployment names as its own."""

    name: str | None = Field(description="Your org's name. Null while none is named.")
    display_name: str | None = Field(
        description="Its display name. Null while none is named or the org is gone."
    )


class YourOrganisationUpdate(BaseModel):
    """The registered org to name as yours."""

    name: str = Field(min_length=1, description="A registered, enabled org's name.")


def _home_etag(name: str | None) -> str | None:
    """The entity-tag your org's doc answers with, None while none is named."""
    return etag_of({"name": name}) if name is not None else None


def _replaceable_home(request: Request, store: HomeOrgStore) -> str | None:
    """The name the doc holds, None when it names none, cannot be read or names an org no longer registered, so a PUT replaces it.

    A config directory lost with its orgs leaves the deploy repo's doc naming one, and the wizard then names a new org.
    """
    try:
        name = store.get()
    except ValueError as exc:
        logger.warning("your-organisation doc is unreadable, a PUT replaces it", error=str(exc))
        return None
    if (name is not None) and (request.app.state.org_registry.get(name) is None):
        logger.warning("your-organisation doc names an org no longer registered", org=name)
        return None
    return name


def _home_store(request: Request) -> HomeOrgStore:
    """The org registry's your-org store, else 503 ``not_configured``."""
    registry = getattr(request.app.state, "org_registry", None)
    if (registry is None) or (registry.home is None):
        raise ProblemError(503, "This engine holds no organisations", code="not_configured")
    return registry.home


def _your_organisation(*, name: str | None, request: Request) -> YourOrganisation:
    """*name* and its display name, as the registry holds it now."""
    org = request.app.state.org_registry.get(name) if name is not None else None
    return YourOrganisation(display_name=org.display_name if org is not None else None, name=name)


@router.get(
    "/organisation",
    response_model=YourOrganisation,
    dependencies=[Depends(require_action(scopes_dict["org_read"]))],
    responses={
        409: {"description": "home_org_unreadable: the doc cannot be read, PUT replaces it"},
    },
)
def get_organisation(user: CurrentUser, request: Request, response: Response) -> YourOrganisation:
    """Your org, the organisation running this deployment. The ETag is absent while none is named."""
    store = _home_store(request)
    try:
        name = request.app.state.org_registry.home_name()
    except ValueError as exc:
        raise ProblemError(
            409,
            f"Your organisation's doc cannot be read. PUT a name with If-None-Match: * "
            f"to replace it: {exc}",
            code="home_org_unreadable",
        ) from exc
    etag = _home_etag(_replaceable_home(request, store))
    if etag is not None:
        response.headers["ETag"] = etag
    return _your_organisation(name=name, request=request)


@router.put(
    "/organisation",
    response_model=YourOrganisation,
    dependencies=[Depends(require_action(scopes_dict["org_write"])), WRITE_TURN],
    responses={
        412: {"description": "A precondition failed: your org changed, or one is already named"},
        422: {"description": "unknown_org: no enabled org of that name is registered"},
        428: {"description": "precondition_required: send If-None-Match: * or If-Match"},
        503: {"description": "not_configured: this engine holds no organisations"},
    },
)
def put_organisation(
    body: YourOrganisationUpdate,
    user: CurrentUser,
    request: Request,
    response: Response,
    preconditions: PreconditionHeaders,
    dry_run: DryRun,
) -> YourOrganisation:
    """Name a registered, enabled org as yours, the organisation running this deployment.

    ``If-None-Match: *`` names the first, or replaces a doc that cannot be read or names an org no longer registered, and ``If-Match`` changes it. With gitops on this
    is a direct commit to the deploy repo's ``governance/settings/organisation.yaml``,
    else that file under the config directory. The apps then recompile, stamping its
    first tenant id on every record that names no org.
    """
    store = _home_store(request)
    preconditions.check(_home_etag(_replaceable_home(request, store)))
    org = request.app.state.org_registry.get(body.name)
    if (org is None) or (not (org.enabled)):
        raise ProblemError(422, f"No enabled org {body.name!r} is registered", code="unknown_org")
    response.headers["ETag"] = etag_of({"name": body.name})
    if dry_run:
        return _your_organisation(name=body.name, request=request)
    store.set(actor=user.user_id, name=body.name)
    audit_org_change(admin_id=user.user_id, change="home", org_name=body.name)
    logger.info("your organisation set via API", actor=user.user_id, org=body.name)
    # The apps stamp your org's tenant id on a record naming no org, so they recompile now.
    reconcile_tenancy(request, user)
    return _your_organisation(name=body.name, request=request)


# -- Table defaults -------------------------------------------


class IntDefault(BaseModel):
    """One integer default: the value in force, and where it came from."""

    effective: int
    stored: int | None = Field(description="The admin's override; null when none is stored.")
    origin: Literal["override", "deployment"]
    deployment_default: int
    maximum: int = Field(description="The largest value a PATCH accepts, so a form can bound it.")


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
    engine: StrDefault = Field(
        description="MergeTree-family variant, without the Replicated or Shared prefix."
    )
    editable: bool = Field(
        description="Whether this deployment can store an override. False without gitops."
    )


class SystemDefaultsPatch(BaseModel):
    """A partial change. Omitted fields stay as they are. Null clears that override."""

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
            maximum=MAX_DEFAULT_TTL_DAYS,
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
    response_model=SystemDefaults,
    dependencies=[Depends(require_action(scopes_dict["system_write"])), WRITE_TURN],
    responses={
        422: {
            "description": "validation_error: a named value cannot be stored",
        },
        503: {
            "description": "not_configured: gitops is off, so there is nowhere to store the value",
        },
    },
)
def patch_defaults(
    body: SystemDefaultsPatch,
    user: CurrentUser,
    request: Request,
    settings: Settings,
) -> SystemDefaults:
    """Store the named defaults. Only ttl_days is applied to live tables.

    Omitted fields are left alone. null clears that override. Common-header type,
    version and engine are what a source inherits the next time it is deployed.
    This request does not rewrite them onto live tables. A patch that names
    ttl_days stores it and returns, and the same background pass as PUT /retention
    applies it.
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
        raise ProblemError(422, str(exc), code="validation_error") from exc
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
    if "ttl_days" in named:
        request_converge(request.app.state)
    return status


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


class ApplyDefaultsPin(BaseModel):
    """One source whose current version was considered for the table defaults."""

    source: str = Field(description="Source name (_source label)")
    version: str | None = Field(
        default=None,
        description="Version id that was pinned or already matched; null when unset",
    )


class ApplyDefaultsResponse(ReviewFields):
    """Which named sources were pinned, which already had these defaults, and their tables."""

    updated: list[ApplyDefaultsPin] = Field(
        description="Sources whose current version was written with the defaults"
    )
    awaiting_review: list[str] = Field(
        default_factory=list,
        description=(
            "Sources whose pin went to a review branch: main still carries them as they "
            "were, so their tables are left alone and are not listed in live"
        ),
    )
    unchanged: list[ApplyDefaultsPin] = Field(
        description="Sources whose current version already matched the defaults"
    )
    live: list[SourceLiveApply] = Field(
        description=(
            "One entry per named source not awaiting review, in request order: what "
            "reached its table."
        )
    )


@router.post(
    "/defaults/apply",
    response_model=ApplyDefaultsResponse,
    dependencies=[Depends(require_action(scopes_dict["source_write"])), WRITE_TURN],
    responses={
        404: {"description": "not_found: a named source does not exist"},
        409: {
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
    response: Response,
) -> ApplyDefaultsResponse:
    """Pin the current TTL, common header and merge engine onto the named sources and their tables.

    Sources that are not in the list are left alone. A source that already stores
    these values is unchanged in the deploy repo. A missing or engine-owned name
    fails the request before any source is written.

    A draft takes the values in place. A deployed version is never rewritten: the
    values land on a new major version, which becomes ``current``. Either way the
    table takes them when that version is deployed.

    A named source whose deployed version already carries the values has its table
    brought to them in this request: the TTL moves and the header's missing
    columns are added. The engine of an existing table needs a rebuild, so it
    reaches new tables only, and existing columns keep their type. ``live``
    reports each source: what reached the table, what did not and why. A
    ClickHouse failure is reported on that source, and the pin stays committed.

    A pin production+team sends to review is listed in ``awaiting_review`` with
    ``review_required`` and the review headers, and its table is left alone until
    the branch merges, since main still carries the source as it was.
    """
    gc = _optional_gitcrud(request)
    ttl = resolve_state(gc, settings)
    table = resolve_defaults(gc, settings)
    loaded: list[Any] = []
    for name in body.sources:
        try:
            source = registry.get_source(name)
        except SourceNotFoundError:
            raise ProblemError(404, f"Source {name!r} not found", code="not_found") from None
        if registry.is_core(name):
            raise ProblemError(
                409,
                str(SourceCoreResourceError(action="modify", source=name)),
                code="conflict",
                source=name,
            ) from None
        loaded.append(source)

    updated: list[ApplyDefaultsPin] = []
    unchanged: list[ApplyDefaultsPin] = []
    held: list[str] = []
    saved: list[Source] = []
    review = ReviewRouting()
    author = git_author(user)
    for source in loaded:
        pin = ApplyDefaultsPin(source=source.source, version=source.current)
        pinned = pin_table_defaults(
            source,
            header_type=table.header_type,
            header_version=table.header_version,
            ttl_days=ttl.effective,
            engine=table.engine,
        )
        if pinned is None:
            unchanged.append(pin)
            saved.append(source)
            continue
        one = ReviewRouting()
        stored = registry.save_source(
            pinned,
            created_by=author,
            description=f"source: apply table defaults to {source.source}",
            routing=one,
        )
        audit_resource_change(
            user.user_id,
            "source",
            source.source,
            "updated",
            {
                "version": stored.current,
                "ttl_days": ttl.effective,
                "common_header_type": table.header_type,
                "common_header_version": table.header_version,
                "engine": table.engine,
                **(routing_audit_detail(one) or {}),
            },
        )
        if one.review_required:
            review.fold(one)
            held.append(stored.source)
            continue
        saved.append(stored)
        updated.append(ApplyDefaultsPin(source=stored.source, version=stored.current))

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
    set_review_headers(response, review_required=review.review_required, pr_url=review.first_pr_url)
    return ApplyDefaultsResponse(
        **review_fields(review),
        updated=updated,
        unchanged=unchanged,
        awaiting_review=held,
        live=[_live_body(o) for o in outcomes],
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
    """The data database's tables, read once, when any source has been deployed. Else none.

    Read within ``clickhouse.interactive_budget_seconds``, so a down server answers
    503 in seconds.
    """
    if not any(source.deployed_version is not None for source in sources):
        return {}
    database = settings.clickhouse.effective_data_database
    try:
        return live_tables(get_interactive_clickhouse_client(settings), database)
    except SchemaApplyError as exc:
        raise backend_failure(
            503,
            "clickhouse_unavailable",
            "the deployed tables could not be read; the engine log has ClickHouse's reason",
            exc,
            event="default drift: deployed tables unreadable",
        ) from exc


@router.get(
    "/defaults/drift",
    openapi_extra=scalo_extra(verb="drift"),
    response_model=Page[SourceDefaultDrift],
    dependencies=[Depends(require_action(scopes_dict["system_read"]))],
    responses={
        503: {
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
    query: ListParams,
    search: str | None = Query(
        None,
        description="Search in source name, drifted field, and stored or live value",
    ),
) -> Page[SourceDefaultDrift]:
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
    name, a drifted field name, or a stored or live value.
    """
    gc = _optional_gitcrud(request)
    ttl = resolve_state(gc, settings)
    table = resolve_defaults(gc, settings)
    landing = landing_table_name()
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
    return paginate([row["item"] for row in rows], query, bound={"search": search})


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
        raise _cloud_error(exc) from exc
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
        raise ProblemError(503, "ClickHouse Cloud is not configured", code="not_configured")
    logger.info("CH Cloud start requested via API", actor=getattr(user, "username", "?"))
    try:
        st = CloudService(cloud).start()
    except CloudServiceError as exc:
        raise _cloud_error(exc) from exc
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
        raise ProblemError(503, "ClickHouse Cloud is not configured", code="not_configured")
    logger.info("CH Cloud stop requested via API", actor=getattr(user, "username", "?"))
    try:
        st = CloudService(cloud).stop()
    except CloudServiceError as exc:
        raise _cloud_error(exc) from exc
    return CloudServiceStateResponse(
        configured=True, id=st.id, name=st.name, state=st.state, is_running=st.is_running
    )
