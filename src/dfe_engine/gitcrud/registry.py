#  Project:      dfe-engine
#  File:         gitcrud/registry.py
#  Purpose:      Registry of Governed Ops resource classes
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Registry mapping a class name to its ResourceClass descriptor.

The default registry seeds the four DFE Governed Ops classes. Directories are the
deploy-repo layout from docs/ARCHITECTURE.md; they are config, not hardcoded
behaviour, so a deployment can override them.
"""

from __future__ import annotations

from collections.abc import Iterable

from .models import ResourceClass


class UnknownResourceClassError(KeyError):
    """Raised when a resource-class name is not registered."""


class ResourceClassRegistry:
    """Holds the known resource classes; resolves a class by name."""

    def __init__(self, classes: Iterable[ResourceClass]) -> None:
        self._by_name: dict[str, ResourceClass] = {c.name: c for c in classes}

    def get(self, name: str) -> ResourceClass:
        try:
            return self._by_name[name]
        except KeyError:
            raise UnknownResourceClassError(name) from None

    def names(self) -> list[str]:
        return sorted(self._by_name)

    def all(self) -> list[ResourceClass]:
        return [self._by_name[n] for n in self.names()]


def default_registry() -> ResourceClassRegistry:
    """The four DFE Governed Ops classes (see docs/ARCHITECTURE.md, Governed Ops)."""
    return ResourceClassRegistry(
        [
            ResourceClass("helmvars", "values", rbac_prefix="helmvars"),
            ResourceClass("hunts", "hunts", rbac_prefix="hunts"),
            ResourceClass("datamodel", "datamodel", rbac_prefix="datamodel"),
            ResourceClass("governance", "governance", rbac_prefix="governance"),
        ]
    )
