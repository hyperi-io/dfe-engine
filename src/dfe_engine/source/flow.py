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
never starts. What an app CAN carry is data in apps.yaml, so a transform gains
the direct transport by shipping its listener and being listed - never by a
condition added here.

See docs/data-plane/source-flow.md for the shape this builds.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from dfe_engine.appmgmt import catalogue
from dfe_engine.appmgmt.catalogue import AppDescriptor
from dfe_engine.source.models import (
    OPERATORS_WITHOUT_OPERAND,
    RULELESS_OPERATORS,
    Source,
)
from dfe_engine.transport import SourceTransport

if TYPE_CHECKING:
    from dfe_engine.settings import DFESettings

ARCHIVER_SERVICE = "dfe-archiver"
FETCHER_SERVICE = "dfe-fetcher"
LOADER_SERVICE = "dfe-loader"

# The receiver picks a destination on field AND value, so a match that tests no
# value cannot name one. The default flow is exempt: it IS the destination
# everything unmatched already goes to.
_UNROUTABLE_ON_DIRECT = OPERATORS_WITHOUT_OPERAND - RULELESS_OPERATORS


class FlowError(ValueError):
    """Raised when a source's stages cannot be run as declared."""


@dataclass(frozen=True, slots=True)
class FlowTransform:
    """The transform stage, when the source has one."""

    app: str
    """Catalogued app running it, e.g. ``dfe-transform-vrl``."""

    instance: str
    """Its deployed name, e.g. ``dfe-transform-vrl-auth``."""

    variant: str | None
    """The compiled-in program it runs, where the app offers a catalogue of them."""

    endpoint: str | None
    """Direct: the address records reach it on. None on the bus."""

    topics: tuple[str, str] | None
    """Bus: the (landing, transformed) topic pair. None on direct."""


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

    origin: str
    """``receiver`` or ``fetcher``."""

    input: str
    """The receiver match that selects the records, or the fetcher instance polling them."""

    transform: FlowTransform | None
    outputs: FlowOutputs
    table: str


def _match_expression(source: Source) -> str:
    """The receiver match as one line, for a report or a console card."""
    match = source.match
    if match is None:
        return ""
    if match.operator == "always":
        return "always"
    if match.operator == "exists":
        return f"{match.field} exists"
    return f"{match.field} {match.operator} {match.value}"


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
) -> AppDescriptor:
    """The app running one stage of this source, or why it cannot run it.

    The ONE place a transport refusal is raised. What an app carries is data in
    apps.yaml, so this never grows a branch per app.
    """
    app = apps.get(service)
    if app is None:
        raise FlowError(
            f"source {source!r} needs {service} to {purpose}, but no such app is catalogued"
        )
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
) -> FlowTransform | None:
    transform = source.transform
    if transform is None:
        return None

    service = catalogue.transform_service(transform.engine)
    app = _stage_app(
        service,
        transport,
        apps,
        source=source.source,
        purpose=f"run its {transform.engine} transform",
    )

    match = source.match
    if transport == "direct" and match is not None and match.operator in _UNROUTABLE_ON_DIRECT:
        raise FlowError(
            f"source {source.source!r} is matched by {match.operator!r}, which tests no value: "
            "on direct the receiver hands a record to its transform by field AND value, so "
            "this source would reach the loader untransformed"
        )

    return FlowTransform(
        app=service,
        instance=catalogue.instance_name(app, source.source),
        variant=transform.variant,
        endpoint=(
            catalogue.push_endpoint(app, source.source, mesh_namespace)
            if transport == "direct"
            else None
        ),
        # topic_load is set exactly when the source has a transform, which is here.
        topics=(source.topic_land, str(source.topic_load)) if transport == "bus" else None,
    )


def _resolve_input(
    source: Source, transport: SourceTransport, apps: dict[str, AppDescriptor]
) -> str:
    """The receiver match that selects the records, or the fetcher that pulls them.

    The receiver is stack-wide and takes every source, so only a fetcher-based
    source has an app to check here.
    """
    if source.origin == "receiver":
        return _match_expression(source)
    fetcher = _stage_app(FETCHER_SERVICE, transport, apps, source=source.source, purpose="fetch it")
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


def _resolve_loader_output(
    source: Source,
    transport: SourceTransport,
    transform: FlowTransform | None,
    apps: dict[str, AppDescriptor],
    settings: DFESettings,
) -> str:
    _stage_app(LOADER_SERVICE, transport, apps, source=source.source, purpose="load it")
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


def resolve_flow(
    source: Source,
    settings: DFESettings,
    catalogue_apps: dict[str, AppDescriptor] | None = None,
) -> SourceFlow:
    """The stages this source's records travel, or the reason they cannot.

    Raises ``FlowError`` when the deployment does not offer the transport the
    source asks for, or when an app in the flow does not carry it - a manifest
    fact per app, so a transport an app ships a listener for stops being refused
    the moment the manifest says so.
    """
    apps = catalogue_apps if catalogue_apps is not None else catalogue.APP_CATALOGUE
    transport = _resolve_transport(source, settings)
    _check_archive(source, transport, apps)

    transform = _resolve_transform(source, transport, apps, _mesh_namespace(settings))
    return SourceFlow(
        source=source.source,
        transport=transport,
        carrier=(
            settings.transport.bus_provider
            if transport == "bus"
            else settings.transport.direct_protocol
        ),
        origin=source.origin,
        input=_resolve_input(source, transport, apps),
        transform=transform,
        outputs=FlowOutputs(
            loader=_resolve_loader_output(source, transport, transform, apps, settings),
            archive=source.archive,
        ),
        table=source.table_name,
    )
