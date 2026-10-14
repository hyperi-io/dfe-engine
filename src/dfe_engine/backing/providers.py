#  Project:      dfe-engine
#  File:         backing/providers.py
#  Purpose:      Read the provider catalogue and derive who runs a backing service
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The provider catalogue, and what a deployment's provider key makes of a service.

The catalogue is DATA whose source of truth is dfe-infra's ``providers.yaml``, so a
new provider, or a change to what one leaves the deploy repo to set, is an edit
there rather than an engine release. It resolves the way the app manifest does:
an explicit path, then ``DFE_PROVIDER_CATALOGUE_FILE``, then the snapshot bundled
in the image.

A service is ``dfe``-run unless the deployer states a provider. A key the catalogue
marks ``provider`` or ``external`` decides it outright. Any other key -- a self-run
one such as ``strimzi``, or one the catalogue does not list -- is ``external``
where the deploy repo declares the service's mode ``external``, because the chart
then deploys nothing, and ``dfe`` otherwise.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from dfe_engine.manifest import ManifestError, manifest_path, read_manifest

CATALOGUE_FILE_ENV = "DFE_PROVIDER_CATALOGUE_FILE"
"""Where a deployment points the engine at its mounted copy of the catalogue."""

BUNDLED_CATALOGUE = Path(__file__).parent / "providers.yaml"
"""Snapshot shipped in the image, taken from the dfe-infra catalogue."""

EXTERNAL_MODE = "external"
"""The chart mode under which a backing-service chart deploys nothing of its own."""


class ManagedBy(StrEnum):
    """Who runs a backing service."""

    DFE = "dfe"
    """DFE runs it, so every value the deploy repo declares reaches it."""

    PROVIDER = "provider"
    """A SaaS whose API DFE knows runs it."""

    EXTERNAL = "external"
    """Someone else runs it, and DFE has no API to it."""


class SetIn(StrEnum):
    """Where a value the deploy repo cannot move is changed instead."""

    DEPLOYMENT = "deployment"
    """A deployment dial sizes it: change the dial and redeploy."""

    PROVIDER = "provider"
    """Only the provider's own console or API moves it."""

    OPERATOR = "operator"
    """Whoever runs the service changes it."""


class ProviderCatalogueError(ManifestError):
    """Raised when the provider catalogue cannot be read or is malformed."""


@dataclass(frozen=True, slots=True)
class ProviderEntry:
    """What one provider key means for one service."""

    managed_by: ManagedBy
    set_in: SetIn | None = None
    read_only: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ServiceProviders:
    """One service's provider keys, and what it refuses when nobody DFE knows runs it."""

    providers: Mapping[str, ProviderEntry] = field(default_factory=dict)
    external_read_only: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Resolution:
    """Who runs a service on this deployment, and which of its values a write cannot move."""

    managed_by: ManagedBy
    set_in: SetIn | None = None
    read_only: tuple[str, ...] = ()

    def refuses(self, path: str) -> bool:
        """Whether a write at *path*, relative to the service's values prefix, is inert.

        A write at a read-only path, below one, or at any parent of one changes a
        value nothing in the deployment reads. An empty *path* is the prefix itself,
        which every read-only value sits under.

        Args:
            path: The dot-path below the service's values prefix; empty for the prefix.

        Returns:
            True when the write would land on a read-only value.
        """
        if not self.read_only:
            return False
        if not path:
            return True
        return any(
            path == ro or path.startswith(f"{ro}.") or ro.startswith(f"{path}.")
            for ro in self.read_only
        )


SELF_RUN = Resolution(managed_by=ManagedBy.DFE)
"""A service DFE runs: every deploy-repo value reaches it, and nothing new is refused."""


def _paths(where: str, raw: object) -> tuple[str, ...]:
    """A list of non-empty dot-paths, refused when any segment is empty."""
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ProviderCatalogueError(f"{where} must be a list of value paths")
    out: list[str] = []
    for item in raw:
        path = str(item).strip() if isinstance(item, str) else ""
        if not path or any(not segment for segment in path.split(".")):
            raise ProviderCatalogueError(
                f"{where} entry {item!r} must be a dot-path below the service prefix, "
                "e.g. replicas or keeper.replicas"
            )
        out.append(path)
    return tuple(out)


def _entry_from(service: str, key: str, raw: object) -> ProviderEntry:
    """One provider key's entry, refused where its fields contradict its managed_by."""
    where = f"{service}.providers.{key}"
    if not isinstance(raw, dict):
        raise ProviderCatalogueError(f"{where} must be a mapping")
    try:
        managed_by = ManagedBy(str(raw.get("managed_by")))
    except ValueError:
        raise ProviderCatalogueError(
            f"{where}.managed_by is {raw.get('managed_by')!r}; valid: "
            f"{', '.join(m.value for m in ManagedBy)}"
        ) from None
    if managed_by is not ManagedBy.PROVIDER:
        # A DFE-run service reaches every value, and an external one takes the
        # service's own external block, so neither may carry a second answer.
        stray = sorted(k for k in ("set_in", "read_only") if k in raw)
        if stray:
            raise ProviderCatalogueError(
                f"{where} is managed_by {managed_by} and may not declare {', '.join(stray)}"
            )
        return ProviderEntry(managed_by=managed_by)
    try:
        set_in = SetIn(str(raw.get("set_in")))
    except ValueError:
        set_in = None
    if set_in not in (SetIn.DEPLOYMENT, SetIn.PROVIDER):
        raise ProviderCatalogueError(
            f"{where}.set_in is {raw.get('set_in')!r}; a provider-managed service is "
            "sized in the deployment or in the provider"
        )
    return ProviderEntry(
        managed_by=managed_by,
        set_in=set_in,
        read_only=_paths(f"{where}.read_only", raw.get("read_only")),
    )


def _service_from(service: str, raw: object) -> ServiceProviders:
    """One service's block: its provider keys and its external read-only set."""
    if not isinstance(raw, dict):
        raise ProviderCatalogueError(f"{service} must be a mapping")
    providers = raw.get("providers") or {}
    if not isinstance(providers, dict):
        raise ProviderCatalogueError(f"{service}.providers must be a mapping of key to entry")
    external = raw.get("external") or {}
    if not isinstance(external, dict):
        raise ProviderCatalogueError(f"{service}.external must be a mapping")
    return ServiceProviders(
        providers={
            str(key): _entry_from(service, str(key), entry) for key, entry in providers.items()
        },
        external_read_only=_paths(f"{service}.external.read_only", external.get("read_only")),
    )


def load_providers(path: Path | str | None = None) -> dict[str, ServiceProviders]:
    """Read the provider catalogue into one block per service.

    Resolution order: the given path, then ``DFE_PROVIDER_CATALOGUE_FILE``, then
    the snapshot bundled in the image, which is always there.

    Args:
        path: An explicit catalogue file; None resolves through the environment.

    Returns:
        Each service the catalogue names, keyed by service.

    Raises:
        ProviderCatalogueError: The file is missing, unreadable or malformed.
    """
    source = manifest_path(path, CATALOGUE_FILE_ENV, BUNDLED_CATALOGUE)
    if source is None:
        raise ProviderCatalogueError("provider catalogue not found: nothing names one")
    try:
        doc = read_manifest(source, what="provider catalogue")
    except ManifestError as exc:
        raise ProviderCatalogueError(str(exc)) from exc
    services = doc.get("services")
    if not isinstance(services, dict) or not services:
        raise ProviderCatalogueError(f"provider catalogue {source} declares no services")
    return {str(name): _service_from(str(name), raw) for name, raw in services.items()}


PROVIDER_CATALOGUE: dict[str, ServiceProviders] = load_providers()


def resolve(
    service: str,
    provider: str,
    mode: Callable[[], object],
    *,
    catalogue: Mapping[str, ServiceProviders] | None = None,
) -> Resolution:
    """Who runs *service* on this deployment, and what a write there cannot move.

    ``mode`` is called only when the provider key leaves the answer open, so a
    deployment that states no provider, or a provider-managed one, never reads
    the deploy repo for it.

    Args:
        service: The backing service, as the API names it.
        provider: The provider key the deployer stated; empty when it stated none.
        mode: Returns the mode the deploy repo declares for the service, or None.
        catalogue: The catalogue to read; the one this process loaded when None.

    Returns:
        The resolution: ``dfe`` with nothing refused, or who runs it and what is refused.
    """
    if not provider:
        return SELF_RUN
    known = (PROVIDER_CATALOGUE if catalogue is None else catalogue).get(service)
    entry = known.providers.get(provider) if known is not None else None
    if entry is not None and entry.managed_by is ManagedBy.PROVIDER:
        return Resolution(ManagedBy.PROVIDER, entry.set_in, entry.read_only)
    stated_external = entry is not None and entry.managed_by is ManagedBy.EXTERNAL
    if stated_external or mode() == EXTERNAL_MODE:
        external = known.external_read_only if known is not None else ()
        return Resolution(ManagedBy.EXTERNAL, SetIn.OPERATOR, external)
    return SELF_RUN
