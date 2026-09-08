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
into the one deployment. An INSTANCE-scoped block (the fetcher) compiles from
the single source the instance is named for, so each fetcher deployment carries
exactly its own source's stanza.

Which apps have compiled routing, which compiler they use, its scope and where
the block lands are all declared in the app manifest, so adding one is a
manifest edit. The registry here maps a declared compiler NAME to the function
that implements it - a new app reusing an existing compiler needs no code at all.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from dfe_engine.gitcrud.engine import get_path, set_path
from dfe_engine.source.registry import SourceNotFoundError, SourceRegistry

from .catalogue import AppDescriptor
from .instances import AppInstance


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
    values_path: str
    compiled: dict[str, Any]
    deployed: dict[str, Any]

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


def _receiver(registry: SourceRegistry, settings: Any, instance: str | None) -> dict[str, Any]:
    from dfe_engine.services.source_routing import compile_receiver_routing

    return compile_receiver_routing(registry).model_dump(mode="json")


def _loader(registry: SourceRegistry, settings: Any, instance: str | None) -> dict[str, Any]:
    from dfe_engine.services.source_routing import compile_loader_routing

    db = settings.clickhouse.effective_data_database
    return compile_loader_routing(registry, db=db).model_dump(mode="json")


def _fetcher(registry: SourceRegistry, settings: Any, instance: str | None) -> dict[str, Any]:
    """The fetcher's ``sources`` block for the one source this instance is named for."""
    if not instance:
        raise RoutingNotApplicableError("a fetcher instance is named for its source")
    try:
        source = registry.get_source(instance)
    except SourceNotFoundError as exc:
        raise RoutingNotApplicableError(f"no source {instance!r} is defined") from exc
    fetcher = source.fetcher
    if fetcher is None:
        raise RoutingNotApplicableError(f"source {instance!r} is receiver-based, not fetched")
    stanza: dict[str, Any] = {
        "enabled": True,
        "topic": fetcher.landing_label(source.source),
    }
    stanza.update(fetcher.config)
    return {fetcher.source_type: stanza}


Compiler = Callable[[SourceRegistry, Any, str | None], dict[str, Any]]

_COMPILERS: dict[str, Compiler] = {
    "receiver": _receiver,
    "loader": _loader,
    "fetcher": _fetcher,
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
    return compiler(registry, settings, instance if app.routing_is_per_instance else None)


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
    deployed = get_path(doc, app.routing_path, default=None) or {}
    return RoutingStatus(
        compiler=app.routing_compiler,
        values_path=app.routing_path,
        compiled=compiled,
        deployed=deployed if isinstance(deployed, dict) else {},
    )


def sync(
    app: AppDescriptor,
    doc: dict,
    registry: SourceRegistry,
    settings: Any,
    *,
    instance: str | None = None,
) -> bool:
    """Write the compiled routing into the overlay. Mutates ``doc``.

    Returns whether anything changed, so an unchanged sync is not a commit.
    """
    current = status(app, doc, registry, settings, instance=instance)
    if not current.drift:
        return False
    set_path(doc, app.routing_path, current.compiled)
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
