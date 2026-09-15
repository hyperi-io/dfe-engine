#  Project:      dfe-engine
#  File:         appmgmt/routing.py
#  Purpose:      Deliver source-derived routing into an app instance's overlay
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Routing an app cannot be told by hand, because the sources already say it.

A source declares how its data enters: a receiver match rule, or a fetcher
that polls it in. ``services/source_routing.py`` turns every source into the
receiver's ``source_rules`` and the loader's table map. What was missing is
DELIVERY: the compiled block had no path into the overlay Argo applies, so a
deployed receiver ran on its built-in defaults and every event landed in the
default topic whatever the sources said.

This module is that path. The compiled block is DERIVED state, so it behaves
like a resolved link rather than an edited value: ``status`` compares what the
sources currently compile to against what the overlay carries, and ``sync``
rewrites the overlay to match. A hand edit is reported as drift instead of being
mistaken for intent.

Two scopes. A STACK-scoped block (receiver, loader) compiles from every source
into the one deployment. An INSTANCE-scoped block (the fetcher, each transform)
compiles from the single source the instance is named for, so each deployment
carries exactly its own source's stanza.

An app carries one or more NAMED blocks, each owned whole: the receiver's rules
and its destination set are separate sections it reads separately, and a block
the compile stops emitting is removed rather than left beside the new one, which
is how moving a source between transports clears the keys of the one it left.

Which apps have compiled routing, which compiler they use, its scope and where
each block lands are all declared in the app manifest, so adding one is a
manifest edit. The registry here maps a declared compiler NAME to the function
that implements it - a new app reusing an existing compiler needs no code at all.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from dfe_engine.gitcrud.engine import del_path, get_path, set_path
from dfe_engine.source.models import Source
from dfe_engine.source.registry import SourceNotFoundError, SourceRegistry

from . import catalogue
from .catalogue import AppDescriptor, CatalogueError
from .instances import AppInstance
from .scaling import DeployTarget

if TYPE_CHECKING:
    from dfe_engine.source.flow import SourceFlow

# The apps' own names for the two transports. A source records bus or direct;
# the compiled config names the mechanism, because that is what the app reads.
BUS_TRANSPORT = "kafka"
DIRECT_TRANSPORT = "grpc"

# A transform's two ends on the direct transport, where the wiring is scalo's
# Push listener and sender and so is the same for every transform.
SOURCE_BLOCK = "source"
SINK_BLOCK = "sink"


class UnknownRoutingCompilerError(KeyError):
    """Raised when the manifest names a compiler this engine does not implement."""


class RoutingNotCompiledError(ValueError):
    """Raised when an app's routing is not derived from the sources."""


class RoutingNotApplicableError(ValueError):
    """Raised when an instance-scoped compiler has no source to compile from.

    The instance names a source that does not exist, or one whose origin is not
    this app's: a fetcher instance for a receiver-based source has nothing to run.
    """


@dataclass(frozen=True, slots=True)
class RoutingStatus:
    """What the sources compile to now, against what the overlay carries."""

    compiler: str
    values_paths: dict[str, str]
    compiled: dict[str, Any]
    """Each derived block the sources call for now, by name."""

    deployed: dict[str, Any]
    """The same blocks as the overlay carries them; a block it lacks is absent here."""

    @property
    def drift(self) -> bool:
        """Whether the overlay disagrees with the sources."""
        return self.compiled != self.deployed

    @property
    def absent(self) -> bool:
        """Whether the overlay carries no routing at all.

        Worth separating from drift: an absent block means the app is running on
        its built-in defaults, which is how a receiver silently ignores every
        source rule ever defined.
        """
        return not self.deployed


def reaches_apps(target: DeployTarget, *, writes_app_config: bool) -> bool:
    """Whether an overlay written here reaches the app that reads it.

    A fact about the DEPLOYMENT rather than a test of its target, because two
    different mechanisms deliver the same overlay. On Kubernetes the GitOps
    controller applies it and the pod rolls onto it. On Compose there is no
    chart, so ``appconfig`` renders each app's own config file into a directory
    the containers mount - and a Compose deployment that wires neither still
    reports false, since its running receiver would keep the routing it started
    with.

    Args:
        target: Where this deployment runs.
        writes_app_config: Whether the engine itself renders the apps' config
            files here, which is ``appconfig.enabled``.
    """
    if target is DeployTarget.DOCKER:
        return writes_app_config
    return True


def _bound_source(registry: SourceRegistry, instance: str | None) -> Source:
    """The source an instance-scoped block compiles from."""
    if not instance:
        raise RoutingNotApplicableError("an instance-scoped app is named for its source")
    try:
        return registry.get_source(instance)
    except SourceNotFoundError as exc:
        raise RoutingNotApplicableError(f"no source {instance!r} is defined") from exc


def _flow(source: Source, settings: Any) -> SourceFlow:
    """This source's resolved stages, or why it has none to compile.

    The resolver holds every transport and endpoint rule, so a compiler asks it
    rather than restating any of them. A source that cannot run is refused at
    save, so reaching a FlowError here means the deployment moved underneath a
    stored source - reported, never compiled around.
    """
    # Imported here because the resolver reaches back into this package for the
    # catalogue: at module level the two would deadlock on whichever loads first.
    from dfe_engine.source.flow import FlowError, resolve_flow

    try:
        return resolve_flow(source, settings)
    except FlowError as exc:
        raise RoutingNotApplicableError(str(exc)) from exc


def _landing(source: Source, settings: Any) -> str:
    """Where records BELONGING to this source are handed to its first stage.

    Its transform when it has one, else the loader. On the bus that is a landing
    label the app suffixes on delivery; on direct it is an endpoint URI.
    """
    flow = _flow(source, settings)
    if flow.transport == "bus":
        return source.landing_label()
    if flow.transform is not None and flow.transform.endpoint:
        return flow.transform.endpoint
    return flow.outputs.loader


def into_blocks(app: AppDescriptor, values: dict[str, object]) -> dict[str, Any]:
    """Group compiled dot-path values into the named blocks the manifest declares."""
    blocks: dict[str, Any] = {}
    for path, value in values.items():
        name, inner = app.block_for(path)
        if not inner:
            blocks[name] = value
        else:
            set_path(blocks.setdefault(name, {}), inner, value)
    return blocks


def _receiver(
    app: AppDescriptor, registry: SourceRegistry, settings: Any, instance: str | None
) -> dict[str, Any]:
    from dfe_engine.services.source_routing import (
        compile_receiver_destinations,
        compile_receiver_routing,
    )

    return {
        "routing": compile_receiver_routing(registry).model_dump(mode="json"),
        "destinations": compile_receiver_destinations(registry, settings).model_dump(mode="json"),
    }


def _loader(
    app: AppDescriptor, registry: SourceRegistry, settings: Any, instance: str | None
) -> dict[str, Any]:
    from dfe_engine.services.source_routing import compile_loader_routing

    db = settings.clickhouse.effective_data_database
    # Only the keys the sources derive: the block is written whole, so a model
    # default in it would overwrite the deployment's own setting for that key.
    compiled = compile_loader_routing(registry, db=db)
    return {"routing": compiled.model_dump(mode="json", exclude_unset=True)}


def _fetcher_route(
    flow: SourceFlow, route: Any, registry: SourceRegistry, settings: Any
) -> dict[str, Any]:
    """One route: which fetched records go to ANOTHER source's landing, and where.

    The target has to exist and be live, because a route to a source with no
    table sends records nowhere; and it has to be on the same transport, because
    a fetcher delivers over one.
    """
    try:
        target = registry.get_source(route.source)
    except SourceNotFoundError as exc:
        raise RoutingNotApplicableError(
            f"source {flow.source!r} routes to {route.source!r}, which is not defined"
        ) from exc
    if target.state != "active":
        raise RoutingNotApplicableError(
            f"source {flow.source!r} routes to {route.source!r}, which is {target.state}"
        )
    if _flow(target, settings).transport != flow.transport:
        raise RoutingNotApplicableError(
            f"source {flow.source!r} routes to {route.source!r}, which is on the other "
            "transport; a fetcher delivers over one"
        )
    key = "topic" if flow.transport == "bus" else "endpoint"
    return {
        "match_field": route.match.field,
        "match_value": route.match.value,
        key: _landing(target, settings),
    }


def _fetcher(
    app: AppDescriptor, registry: SourceRegistry, settings: Any, instance: str | None
) -> dict[str, Any]:
    """What this fetcher instance polls, and where the records it pulls are sent."""
    source = _bound_source(registry, instance)
    fetcher = source.fetcher
    if fetcher is None:
        raise RoutingNotApplicableError(f"source {instance!r} is receiver-based, not fetched")
    flow = _flow(source, settings)

    stanza: dict[str, Any] = {
        "enabled": True,
        # The bare landing label: the fetcher appends its own topic suffix.
        "topic": fetcher.landing_label(source.source),
    }
    stanza.update(fetcher.config)

    output: dict[str, Any] = {"type": BUS_TRANSPORT}
    if flow.transport == "direct":
        output = {
            "type": DIRECT_TRANSPORT,
            DIRECT_TRANSPORT: {"endpoint": _landing(source, settings)},
        }
    routes = [_fetcher_route(flow, r, registry, settings) for r in fetcher.routes]
    if routes:
        output["routes"] = routes

    return {"sources": {fetcher.source_type: stanza}, "output": output}


def _transform(
    app: AppDescriptor, registry: SourceRegistry, settings: Any, instance: str | None
) -> dict[str, Any]:
    """One transform instance's input and output, for the source it is named for.

    The bus wiring is the manifest's ``source_binding``, because each transform
    names its topics and consumer group differently. The direct wiring is the
    same for all of them, being scalo's Push listener and sender.
    """
    source = _bound_source(registry, instance)
    flow = _flow(source, settings)
    if flow.transform is None:
        raise RoutingNotApplicableError(f"source {instance!r} has no transform stage")
    if flow.transform.app != app.service:
        raise RoutingNotApplicableError(
            f"source {instance!r} is transformed by {flow.transform.app}, not {app.service}"
        )

    if flow.transport == "bus":
        blocks = into_blocks(app, catalogue.render_source_binding(app, source.source))
    else:
        missing = {SOURCE_BLOCK, SINK_BLOCK} - set(app.routing_paths)
        if missing:
            raise CatalogueError(
                f"{app.service} carries the direct transport but declares no "
                f"{', '.join(sorted(missing))} block to wire its listener into"
            )
        blocks = {
            SOURCE_BLOCK: {"transport": DIRECT_TRANSPORT, "listen": catalogue.push_listen(app)},
            SINK_BLOCK: {"transport": DIRECT_TRANSPORT, "endpoint": flow.outputs.loader},
        }
    if app.variant_path and flow.transform.variant:
        name, inner = app.block_for(app.variant_path)
        set_path(blocks.setdefault(name, {}), inner, flow.transform.variant)
    return blocks


Compiler = Callable[[AppDescriptor, SourceRegistry, Any, str | None], dict[str, Any]]

# Named because callers outside the compile path select the loading stage by it.
LOADER_COMPILER = "loader"

_COMPILERS: dict[str, Compiler] = {
    "receiver": _receiver,
    LOADER_COMPILER: _loader,
    "fetcher": _fetcher,
    "transform": _transform,
}


def compilers() -> list[str]:
    """Every compiler name the manifest may declare."""
    return sorted(_COMPILERS)


def compile_for(
    app: AppDescriptor,
    registry: SourceRegistry,
    settings: Any,
    *,
    instance: str | None = None,
) -> dict[str, Any]:
    """What this app's routing block should be, given the current sources.

    ``instance`` is the bound source for an instance-scoped app and ignored by a
    stack-scoped one.
    """
    if not app.has_compiled_routing:
        raise RoutingNotCompiledError(
            f"{app.service} declares no routing compiler; its routing is not "
            "derived from the source definitions"
        )
    try:
        compiler = _COMPILERS[app.routing_compiler]
    except KeyError:
        raise UnknownRoutingCompilerError(app.routing_compiler) from None
    blocks = compiler(app, registry, settings, instance if app.routing_is_per_instance else None)
    undeclared = set(blocks) - set(app.routing_paths)
    if undeclared:
        raise CatalogueError(
            f"{app.service}: compiler {app.routing_compiler!r} emitted "
            f"{', '.join(sorted(undeclared))}, which the manifest gives no overlay path"
        )
    return blocks


def status(
    app: AppDescriptor,
    doc: dict,
    registry: SourceRegistry,
    settings: Any,
    *,
    instance: str | None = None,
) -> RoutingStatus:
    """Compare the compiled routing against what the overlay carries."""
    compiled = compile_for(app, registry, settings, instance=instance)
    deployed: dict[str, Any] = {}
    for name, path in app.routing_paths.items():
        found = get_path(doc, path, default=None)
        if found is not None:
            deployed[name] = found
    return RoutingStatus(
        compiler=app.routing_compiler,
        values_paths=dict(app.routing_paths),
        compiled=compiled,
        deployed=deployed,
    )


def apply(app: AppDescriptor, doc: dict, compiled: dict[str, Any]) -> None:
    """Write the compiled blocks into the overlay. Mutates ``doc``.

    The one writer of derived state: a block the compile no longer emits is
    removed, so nothing survives a source moving between transports.
    """
    for name, path in app.routing_paths.items():
        if name in compiled:
            set_path(doc, path, compiled[name])
        else:
            del_path(doc, path)


def sync(
    app: AppDescriptor,
    doc: dict,
    registry: SourceRegistry,
    settings: Any,
    *,
    instance: str | None = None,
) -> bool:
    """Bring the overlay into step with the sources. Mutates ``doc``.

    Returns whether anything changed, so an unchanged sync is not a commit.
    """
    current = status(app, doc, registry, settings, instance=instance)
    if not current.drift:
        return False
    apply(app, doc, current.compiled)
    return True


def instances_needing_sync(
    apps: list[tuple[AppInstance, dict]], registry: SourceRegistry, settings: Any
) -> list[tuple[AppInstance, RoutingStatus]]:
    """Every instance whose overlay routing disagrees with the sources.

    An instance-scoped app whose source is gone is skipped here: it has nothing
    to sync to, and removing it is the derived-deployment reconcile's call.
    """
    out: list[tuple[AppInstance, RoutingStatus]] = []
    for app, doc in apps:
        if not app.descriptor.has_compiled_routing:
            continue
        try:
            found = status(app.descriptor, doc, registry, settings, instance=app.instance)
        except RoutingNotApplicableError:
            continue
        if found.drift:
            out.append((app, found))
    return out
