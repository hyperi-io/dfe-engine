#  Project:      dfe-engine
#  File:         gitcrud/registry.py
#  Purpose:      Registry of Governed Ops resource classes
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Registry mapping a class name to its ResourceClass descriptor.

The default registry seeds the DFE Governed Ops resource classes. Directories are the
deploy-repo layout from docs/architecture.md; they are config, not hardcoded
behaviour, so a deployment can override them.
"""

from __future__ import annotations

from collections.abc import Iterable

from .models import ResourceClass


class UnknownResourceClassError(KeyError):
    """Raised when a resource-class name is not registered."""


class ResourceClassRegistry:
    """Holds the known resource types; resolves one by name.

    Each registered ResourceClass is a resource TYPE (one directory); several types
    share an RBAC `rbac_prefix` (the high-level CLASS). RBAC is bound at the class
    level (the prefix), so a `governance:write` grant covers every governance type
    (accounts/groups/roles/actions/policies) - never per-file.
    """

    def __init__(self, classes: Iterable[ResourceClass]) -> None:
        self._by_name: dict[str, ResourceClass] = {c.name: c for c in classes}

    def get(self, name: str) -> ResourceClass:
        try:
            return self._by_name[name]
        except KeyError:
            raise UnknownResourceClassError(name) from None

    def names(self) -> list[str]:
        """All resource-type names."""
        return sorted(self._by_name)

    def all(self) -> list[ResourceClass]:
        return [self._by_name[n] for n in self.names()]

    def classes(self) -> list[str]:
        """Distinct RBAC class prefixes (the high-level RBAC handles)."""
        return sorted({c.rbac_prefix or c.name for c in self._by_name.values()})


def default_registry() -> ResourceClassRegistry:
    """DFE Governed Ops resource types for the DEPLOY repo (see docs/architecture.md).

    Typed entries sharing RBAC class prefixes. Scope here is the deploy repo:
    `helmvars` (overlays) + the `governance` class (rbac + actions + policies).
    The `datamodel` (sources/schemas/fieldmaps) and `hunts` (defs/rules/alert-dests)
    classes live in the config-SSoT repo and arrive with the multi-repo work
    (option A); they are a second registry over that repo.
    """
    return ResourceClassRegistry(
        [
            # helmvars class - deployment overlays
            ResourceClass("helmvars", "values", rbac_prefix="helmvars"),
            # governance class - RBAC + curated actions + protected-var policies
            ResourceClass("accounts", "governance/rbac/accounts", rbac_prefix="governance"),
            ResourceClass("groups", "governance/rbac/groups", rbac_prefix="governance"),
            ResourceClass("roles", "governance/rbac/roles", rbac_prefix="governance"),
            ResourceClass("actions", "governance/actions", rbac_prefix="governance"),
            ResourceClass("policies", "governance/policies", rbac_prefix="governance"),
            # CH RBAC - quota tiers + fixed service roles, reconciled to real CH
            # objects (settings profile + quota + role). Versioned like schemas.
            ResourceClass(
                "ch_tiers", "governance/ch/tiers", rbac_prefix="governance", versioned=True
            ),
            ResourceClass(
                "ch_service_roles",
                "governance/ch/service-roles",
                rbac_prefix="governance",
                versioned=True,
            ),
            # engine-wide gitops settings (the auto-merge flag lives here)
            ResourceClass("gov_settings", "governance/settings", rbac_prefix="governance"),
        ]
    )
