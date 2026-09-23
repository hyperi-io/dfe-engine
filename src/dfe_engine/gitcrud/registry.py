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

from .models import Layout, ResourceClass


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
    `helmvars` (overlays), the `governance` class (rbac + actions + policies),
    `library` (versioned artefacts an instance links its consumed files to),
    `sources` (the all-in-one source-definition doc under config/sources - the
    first datamodel class to land in the deploy repo), `derived_schemas` (the
    column selections those sources name), and `hunts` + `rules` (what the hunt
    runner executes). Meta schemas and fieldmaps stay in the dfe-schemas tree;
    hunt alert-destinations arrive with the multi-repo work (option A).
    """
    return ResourceClassRegistry(
        [
            # helmvars class - deployment overlays
            ResourceClass("helmvars", "values", rbac_prefix="helmvars"),
            # The same class for the charts that are NOT app instances - the
            # substrate and platform overlays. A separate DIRECTORY because
            # anything under values/ becomes an Argo application: the appset's
            # `values/*-values.yaml` glob reaches git as a pathspec, where `*`
            # matches `/`, so a subdirectory does not hide a file from it.
            ResourceClass("infravars", "infra", rbac_prefix="helmvars"),
            # datamodel class - source definitions. The stored doc IS the
            # exchange format (API payload = file = gitcrud doc); the Source
            # model owns its own semver version envelope (current/versions/
            # deployed_version), which is what versioned=True declares here.
            ResourceClass("sources", "config/sources", rbac_prefix="sources", versioned=True),
            # datamodel class - a deployment's own column selections over a meta
            # schema, versioned and audited beside the sources that name them.
            # nested: the registry key carries a group (beats/filebeat_auth).
            # versioned: the doc owns a semver envelope, as the Source model does.
            ResourceClass(
                "derived_schemas",
                "config/schemas/derived",
                rbac_prefix="schema",
                versioned=True,
                nested=True,
            ),
            # hunt classes - the hunt definitions and the detection rules they
            # name. The hunt runner reads both directories off a git-sync of
            # this repo (dfe-infra#212), so they are unversioned and the stored
            # doc is exactly what the runner parses. rbac_prefix reuses the
            # engine's existing hunt:/rule: grants rather than minting new ones.
            ResourceClass("hunts", "config/hunts", rbac_prefix="hunt"),
            ResourceClass("rules", "config/rules", rbac_prefix="rule"),
            # library class - the versioned artefact library an app instance links
            # its consumed files to. Versioned because a linked artefact is only
            # useful if an earlier version can be pointed at again, and a BUNDLE
            # because it stores authored content: every version is a real file
            # with a real extension, so a reviewer diffs the language itself.
            ResourceClass(
                "library",
                "config/library",
                rbac_prefix="library",
                versioned=True,
                layout=Layout.BUNDLE,
            ),
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
