#  Project:      dfe-engine
#  File:         source/flow.py
#  Purpose:      Resolve a source into the stages its records actually travel
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""One source in, its whole path out: INPUT, optional TRANSFORM, OUTPUT.

A source says where its records come from, whether they are transformed, and
whether the raw copy is kept. Everything else - the topics, the endpoints, the
instance that runs each stage - follows from that plus the deployment, and this
is where it follows from. The compilers write the same answer into each app's
values, and the console draws it, so both read one resolver rather than two
copies of the convention.

Every refusal is here for the same reason: a flow that cannot run must fail at
save, where the person who typed it is still looking, not at a pod that then
never starts. What an app CAN carry, which profiles deploy it and whether it runs
one deployment or one per source are all data in apps.yaml, so a transform gains
the direct transport by shipping its listener and being listed - never by a
condition added here.

See docs/data-plane/source-flow.md for the shape this builds.
"""

from collections.abc import Collection
from dataclasses import dataclass
from typing import TYPE_CHECKING

from dfe_engine.appmgmt import catalogue
from dfe_engine.appmgmt.catalogue import AppDescriptor, Multiplicity
from dfe_engine.appmgmt.scaling import DeployTarget, instance_ceiling
from dfe_engine.source.models import EXTERNAL_ENGINE, Source, external_consumer_group
from dfe_engine.transport import SourceTransport

if TYPE_CHECKING:
    from dfe_engine.settings import DFESettings

ARCHIVER_SERVICE = "dfe-archiver"
FETCHER_SERVICE = "dfe-fetcher"
LOADER_SERVICE = "dfe-loader"

PER_SOURCE_DOCS = (
    "https://github.com/hyperi-io/dfe-docker/blob/main/docs/profiles.md#per-source-apps"
)
"""Where a Compose operator reads how a checkout runs one container per source."""


class FlowError(ValueError):
    """Raised when a source's stages cannot be run as declared."""


def runs_per_source_apps(transport: SourceTransport) -> bool:
    """Whether a source on *transport* may have an app of its own (a fetcher or a transform).

    Only the bus does. On direct each per-source pool needs the mesh to be
    addressed, and the mesh is not GA, so a direct deployment runs the
    receiver to loader path alone.
    """
    return transport == "bus"


def stage_instance_ceiling(app: AppDescriptor, settings: DFESettings) -> int | None:
    """How many deployments of *app* this deployment can run, None meaning unbounded.

    The one reader of both deployment facts the cap depends on, so the save path
    and the deploy-repo check cannot disagree about how many a target holds.
    """
    from dfe_engine.appmgmt import appconfig

    return instance_ceiling(
        app,
        DeployTarget(settings.deployment.target),
        writes_app_config=appconfig.enabled(settings),
        reads_instance_index=appconfig.custom_env_dir(settings) is not None,
        hosted=_hosted(settings).get(app.service, ()),
    )


def _hosted(settings: DFESettings) -> dict[str, frozenset[str]]:
    """The instances each per-config app has a container for here, by app."""
    from dfe_engine.settings import parse_hosted_instances

    return parse_hosted_instances(settings.deployment.hosted_instances)


def hosting_refusal(app: AppDescriptor, source: str, purpose: str, settings: DFESettings) -> str:
    """Why this deployment cannot run *source*'s instance of *app*, empty where it can."""
    from dfe_engine.appmgmt import appconfig

    ceiling = stage_instance_ceiling(app, settings)
    hosted = _hosted(settings).get(app.service, frozenset())
    if app.multiplicity is not Multiplicity.PER_CONFIG or ceiling is None:
        return ""
    if ceiling and source in hosted:
        return ""
    service = app.service
    if app.shared_instance or not appconfig.enabled(settings):
        return (
            f"source {source!r} needs {service} to {purpose}, and this deployment renders "
            f"no config into its {service} container, so the source would be saved and "
            "never run. Set DFE_ENGINE_DEPLOYMENT_APP_CONFIG_DIR to the directory that "
            "container mounts"
        )
    running = ", ".join(sorted(hosted)) or "none"
    return (
        f"source {source!r} needs its own {service} to {purpose}, and this install runs "
        f"{service} only for the sources it was published with ({running}). A new one "
        f"needs a dfe-docker checkout, where `make apply` starts a {service} per source: "
        f"{PER_SOURCE_DOCS}"
    )


@dataclass(frozen=True, slots=True)
class FlowTransform:
    """The transform stage, when the source has one."""

    engine: str
    """The engine the source names, ``external`` for the customer's own pipeline."""

    app: str | None
    """Catalogued app running it, e.g. ``dfe-transform-vrl``. None for an external transform."""

    instance: str | None
    """Its deployed name, e.g. ``dfe-transform-vrl-auth``. None for an external transform."""

    variant: str | None
    """The compiled-in program it runs, where the app offers a catalogue of them."""

    endpoint: str | None
    """Direct: the address records reach it on. None on the bus."""

    topics: tuple[str, str] | None
    """Bus: the (landing, transformed) topic pair. None on direct."""

    group: str | None = None
    """Bus: the consumer group it reads the landing topic in. None on direct."""


@dataclass(frozen=True, slots=True)
class FlowOutputs:
    """Where the records end up."""

    loader: str
    """The topic the loader consumes, or the endpoint it is pushed to."""

    archive: bool
    """Whether the archiver also keeps the raw record off the landing topic."""


@dataclass(frozen=True, slots=True)
class SourceFlow:
    """One source's whole path, resolved against one deployment."""

    source: str
    transport: SourceTransport

    carrier: str
    """What actually carries it here - the bus provider, or the direct protocol.

    A deployment fact, never a source's choice, and the label the console puts on
    the arrow between two stages.
    """

    origin: str | None
    """``receiver``, ``fetcher``, ``external``, or None when nothing selects the records."""

    input: str
    """The receiver match, the fetcher instance, or the landing topic an external pipeline writes."""

    transform: FlowTransform | None
    outputs: FlowOutputs
    table: str


def _match_expression(source: Source) -> str:
    """The receiver match or condition as one line, for a report or a console card."""
    if source.when is not None:
        return f"when {source.when}"
    match = source.match
    return "" if match is None else match.describe()


def _resolve_transport(source: Source, settings: DFESettings) -> SourceTransport:
    declared = source.transport
    effective: SourceTransport = declared or settings.transport.default
    available = settings.transport.available()
    if effective not in available:
        raise FlowError(
            f"source {source.source!r} asks for the {effective} transport, but this "
            f"deployment offers {', '.join(sorted(available))}"
        )
    return effective


def _stage_app(
    service: str,
    transport: SourceTransport,
    apps: dict[str, AppDescriptor],
    *,
    source: str,
    purpose: str,
    settings: DFESettings,
) -> AppDescriptor:
    """The app running one stage of this source, or why it cannot run it.

    The ONE place a stage refusal is raised. Which profiles may deploy an app,
    what it carries and how many of it a target can run are all data in
    apps.yaml, so this never grows a branch per app.

    The offer is checked first because it is the wider statement: a tier that
    deploys nothing to run this stage cannot run the source on either transport.
    A deployment that names no profile refuses nothing, so a hand-run engine is
    unchanged.
    """
    app = apps.get(service)
    profile = settings.deployment.profile
    if app is None:
        raise FlowError(
            f"source {source!r} needs {service} to {purpose}, but no such app is catalogued"
        )
    if not app.offered_in(profile):
        raise FlowError(
            f"source {source!r} needs {service} to {purpose}, but the {profile} "
            f"profile does not deploy {service}"
        )
    if app.multiplicity is Multiplicity.PER_CONFIG and not runs_per_source_apps(transport):
        raise FlowError(
            f"source {source!r} needs its own {service} to {purpose}, but this deployment "
            f"runs on {settings.transport.direct_protocol}. Sources that need a per-source "
            f"app (fetcher, transform) need a {settings.transport.bus_provider} deployment"
        )
    refusal = hosting_refusal(app, source, purpose, settings)
    if refusal:
        raise FlowError(refusal)
    if not app.carries(transport):
        raise FlowError(
            f"source {source!r} is on the {transport} transport, but {service} "
            f"carries only {', '.join(sorted(app.transports))}"
        )
    return app


def _mesh_namespace(settings: DFESettings) -> str:
    """Where this deployment's pool listeners live, or empty when it has none.

    The one reader of the two deployment facts, so every address in a flow is
    built the same way: off the mesh a sender dials a stage's own Service, on it
    the listener alias fronting that stage's pool.
    """
    transport = settings.transport
    return transport.mesh_namespace if transport.mesh_enabled else ""


def _resolve_transform(
    source: Source,
    transport: SourceTransport,
    apps: dict[str, AppDescriptor],
    mesh_namespace: str,
    settings: DFESettings,
) -> FlowTransform | None:
    transform = source.transform
    if transform is None:
        return None
    # topic_load is set exactly when the source has a transform, which is here.
    topics = (source.topic_land, str(source.topic_load))

    if transform.engine == EXTERNAL_ENGINE:
        _require_bus(source, transport, settings, stage="its external transform reads")
        return FlowTransform(
            engine=EXTERNAL_ENGINE,
            app=None,
            instance=None,
            variant=None,
            endpoint=None,
            topics=topics,
            group=external_consumer_group(source.source),
        )

    service = catalogue.transform_service(transform.engine)
    app = _stage_app(
        service,
        transport,
        apps,
        source=source.source,
        purpose=f"run its {transform.engine} transform",
        settings=settings,
    )
    on_bus = transport == "bus"
    return FlowTransform(
        engine=transform.engine,
        app=service,
        instance=catalogue.instance_name(app, source.source),
        variant=transform.variant,
        endpoint=None if on_bus else catalogue.push_endpoint(app, source.source, mesh_namespace),
        topics=topics if on_bus else None,
        group=catalogue.transform_group(app, source.source) if on_bus else None,
    )


def _require_bus(
    source: Source, transport: SourceTransport, settings: DFESettings, *, stage: str
) -> None:
    """Refuse an external stage off the bus: the customer's pipeline meets DFE at a topic."""
    if transport == "bus":
        return
    raise FlowError(
        f"source {source.source!r} is on the {settings.transport.direct_protocol} "
        f"transport, but {stage} {source.topic_land}, a "
        f"{settings.transport.bus_provider} topic. A pipeline the customer runs plugs in "
        "on the bus only: set transport to bus"
    )


def _resolve_input(
    source: Source,
    transport: SourceTransport,
    apps: dict[str, AppDescriptor],
    settings: DFESettings,
) -> str:
    """The receiver match that selects the records, the fetcher that pulls them, or the topic.

    The receiver is stack-wide and takes every source, so only a fetcher-based
    source has an app to check here. An external pipeline runs nothing of ours,
    so its input is the landing topic it produces to.
    """
    if source.origin is None:
        # Nothing selects these records. They arrived through a receiver or a fetcher like any other and reached this table because the loader had nowhere else to send them.
        return "records the loader could not route elsewhere"
    if source.origin == "receiver":
        return _match_expression(source)
    if source.origin == "external":
        _require_bus(source, transport, settings, stage="its external pipeline writes")
        return source.topic_land
    fetcher = _stage_app(
        FETCHER_SERVICE,
        transport,
        apps,
        source=source.source,
        purpose="fetch it",
        settings=settings,
    )
    return catalogue.instance_name(fetcher, source.source)


def loader_endpoint(
    settings: DFESettings, catalogue_apps: dict[str, AppDescriptor] | None = None
) -> str:
    """Where any direct-transport stage pushes records for the loader.

    Stack-wide, so it takes no source: every direct flow ends at the same
    address, and the receiver's destination set names it once. It still takes the
    deployment, because the loader is a pool like any other stage and the mesh
    fronts it the same way.
    """
    apps = catalogue_apps if catalogue_apps is not None else catalogue.APP_CATALOGUE
    return catalogue.push_endpoint(apps[LOADER_SERVICE], "", _mesh_namespace(settings))


def archiver_endpoint(
    settings: DFESettings, catalogue_apps: dict[str, AppDescriptor] | None = None
) -> str:
    """Where a direct-transport sender pushes the raw copy of an archived record.

    Stack-wide like the loader's: one archiver takes every archived source, so the
    receiver's destination set names it once whatever the source.
    """
    apps = catalogue_apps if catalogue_apps is not None else catalogue.APP_CATALOGUE
    return catalogue.push_endpoint(apps[ARCHIVER_SERVICE], "", _mesh_namespace(settings))


def _resolve_loader_output(
    source: Source,
    transport: SourceTransport,
    transform: FlowTransform | None,
    apps: dict[str, AppDescriptor],
    settings: DFESettings,
) -> str:
    _stage_app(
        LOADER_SERVICE,
        transport,
        apps,
        source=source.source,
        purpose="load it",
        settings=settings,
    )
    if transport == "direct":
        return loader_endpoint(settings, apps)
    # On the bus the loader reads whichever topic the last stage wrote.
    return transform.topics[1] if transform and transform.topics else source.topic_land


def _check_archive(
    source: Source, transport: SourceTransport, apps: dict[str, AppDescriptor]
) -> None:
    """Archiving is opt-in, so its refusal names what the source asked for."""
    if not source.archive:
        return
    archiver = apps.get(ARCHIVER_SERVICE)
    if archiver is None:
        raise FlowError(
            f"source {source.source!r} asks to be archived, but {ARCHIVER_SERVICE} is not catalogued"
        )
    if not archiver.carries(transport):
        raise FlowError(
            f"source {source.source!r} asks to be archived: archive needs the "
            f"{' or '.join(sorted(archiver.transports))} transport, because "
            f"{ARCHIVER_SERVICE} keeps the record off the landing topic"
        )


def archiver_offer_gap(
    flow: SourceFlow,
    settings: DFESettings,
    catalogue_apps: dict[str, AppDescriptor] | None = None,
    deployed: Collection[str] = (),
) -> str | None:
    """Why this flow asks to be archived where nothing runs an archiver, or None.

    The same offer ``_stage_app`` reads for every other stage, plus whether the
    profile runs the archiver unasked or the operator has deployed one. Kept out
    of ``resolve_flow`` because that also compiles stored sources: one stored
    before the deployment moved keeps compiling its routing and still reads.

    Args:
        flow: The resolved flow.
        settings: The deployment's settings.
        catalogue_apps: The app catalogue to judge against, the shipped one by default.
        deployed: The services with an instance in the deploy repo.

    Returns:
        The reason, or None where an archiver runs or the profile is unknown.
    """
    if not flow.outputs.archive:
        return None
    apps = catalogue_apps if catalogue_apps is not None else catalogue.APP_CATALOGUE
    archiver = apps.get(ARCHIVER_SERVICE)
    profile = settings.deployment.profile
    if archiver is None or not profile or ARCHIVER_SERVICE in deployed:
        return None
    if not archiver.offered_in(profile):
        return (
            f"source {flow.source!r} asks to be archived, but the {profile} profile does not "
            f"deploy {ARCHIVER_SERVICE}, so nothing would keep its records: set archive to "
            "false, or archive it on a profile that deploys the archiver"
        )
    if archiver.runs_unasked_in(profile):
        return None
    return (
        f"source {flow.source!r} asks to be archived, but the {profile} profile does not run "
        f"{ARCHIVER_SERVICE} unless it is deployed, and none is, so the receiver would hold "
        "every record waiting on it: set archive to false, or deploy the archiver first"
    )


def resolve_flow(
    source: Source,
    settings: DFESettings,
    catalogue_apps: dict[str, AppDescriptor] | None = None,
) -> SourceFlow:
    """The stages this source's records travel, or the reason they cannot.

    Raises ``FlowError`` when the deployment does not offer the transport the
    source asks for, when this deployment's profile deploys no app to run one of
    its stages, when the target cannot run an instance of a per-config stage, or
    when an app in the flow does not carry the transport - all manifest facts per
    app, so an app stops being refused the moment the manifest says so.
    """
    apps = catalogue_apps if catalogue_apps is not None else catalogue.APP_CATALOGUE
    transport = _resolve_transport(source, settings)
    _check_archive(source, transport, apps)

    transform = _resolve_transform(source, transport, apps, _mesh_namespace(settings), settings)
    return SourceFlow(
        source=source.source,
        transport=transport,
        carrier=(
            settings.transport.bus_provider
            if transport == "bus"
            else settings.transport.direct_protocol
        ),
        origin=source.origin,
        input=_resolve_input(source, transport, apps, settings),
        transform=transform,
        outputs=FlowOutputs(
            loader=_resolve_loader_output(source, transport, transform, apps, settings),
            archive=source.archive,
        ),
        table=source.table_name,
    )
