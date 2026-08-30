#  Project:      dfe-engine
#  File:         appmgmt/routing.py
#  Purpose:      Deliver source-derived routing into an app instance's overlay
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Routing an app cannot be told by hand, because the sources already say it.

A source declares the receiver match rule that identifies it, and that rule is
required - a source cannot exist without one. ``services/source_routing.py``
turns every source into the receiver's ``source_rules`` and the loader's table
map. What was missing is DELIVERY: the compiled block had no path into the
overlay Argo applies, so a deployed receiver ran on its built-in defaults and
every event landed in the default topic whatever the sources said.

This module is that path. The compiled block is DERIVED state, so it behaves
like a resolved link rather than an edited value: ``status`` compares what the
sources currently compile to against what the overlay carries, and ``sync``
rewrites the overlay to match. A hand edit is reported as drift instead of being
mistaken for intent.

Which apps have compiled routing, which compiler they use and where the block
lands are all declared in the app manifest, so adding one is a manifest edit.
The registry here maps a declared compiler NAME to the function that implements
it - a new app reusing an existing compiler needs no code at all.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from dfe_engine.gitcrud.engine import get_path, set_path
from dfe_engine.source.registry import SourceRegistry

from .catalogue import AppDescriptor
from .instances import AppInstance


class UnknownRoutingCompilerError(KeyError):
    """Raised when the manifest names a compiler this engine does not implement."""


class RoutingNotCompiledError(ValueError):
    """Raised when an app's routing is not derived from the sources."""


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


def _receiver(registry: SourceRegistry, settings: Any) -> dict[str, Any]:
    from dfe_engine.services.source_routing import compile_receiver_routing

    return compile_receiver_routing(registry).model_dump(mode="json")


def _loader(registry: SourceRegistry, settings: Any) -> dict[str, Any]:
    from dfe_engine.services.source_routing import compile_loader_routing

    db = settings.clickhouse.effective_data_database
    return compile_loader_routing(registry, db=db).model_dump(mode="json")


Compiler = Callable[[SourceRegistry, Any], dict[str, Any]]

_COMPILERS: dict[str, Compiler] = {
    "receiver": _receiver,
    "loader": _loader,
}


def compilers() -> list[str]:
    """Every compiler name the manifest may declare."""
    return sorted(_COMPILERS)


def compile_for(app: AppDescriptor, registry: SourceRegistry, settings: Any) -> dict[str, Any]:
    """What this app's routing block should be, given the current sources."""
    if not app.has_compiled_routing:
        raise RoutingNotCompiledError(
            f"{app.service} declares no routing compiler; its routing is not "
            "derived from the source definitions"
        )
    try:
        compiler = _COMPILERS[app.routing_compiler]
    except KeyError:
        raise UnknownRoutingCompilerError(app.routing_compiler) from None
    return compiler(registry, settings)


def status(app: AppDescriptor, doc: dict, registry: SourceRegistry, settings: Any) -> RoutingStatus:
    """Compare the compiled routing against what the overlay carries."""
    compiled = compile_for(app, registry, settings)
    deployed = get_path(doc, app.routing_path, default=None) or {}
    return RoutingStatus(
        compiler=app.routing_compiler,
        values_path=app.routing_path,
        compiled=compiled,
        deployed=deployed if isinstance(deployed, dict) else {},
    )


def sync(app: AppDescriptor, doc: dict, registry: SourceRegistry, settings: Any) -> bool:
    """Write the compiled routing into the overlay. Mutates ``doc``.

    Returns whether anything changed, so an unchanged sync is not a commit.
    """
    current = status(app, doc, registry, settings)
    if not current.drift:
        return False
    set_path(doc, app.routing_path, current.compiled)
    return True


def instances_needing_sync(
    apps: list[tuple[AppInstance, dict]], registry: SourceRegistry, settings: Any
) -> list[tuple[AppInstance, RoutingStatus]]:
    """Every instance whose overlay routing disagrees with the sources."""
    out: list[tuple[AppInstance, RoutingStatus]] = []
    for app, doc in apps:
        if not app.descriptor.has_compiled_routing:
            continue
        found = status(app.descriptor, doc, registry, settings)
        if found.drift:
            out.append((app, found))
    return out
