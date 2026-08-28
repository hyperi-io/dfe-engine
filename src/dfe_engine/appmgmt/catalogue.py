#  Project:      dfe-engine
#  File:         appmgmt/catalogue.py
#  Purpose:      The deployed-app catalogue - what the generic layer knows per app
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""What the app-management layer needs to know about each deployed DFE app.

The per-app facts are DATA, read from the ``apps.yaml`` manifest whose source of
truth is dfe-infra, so adding or changing an app is an edit there rather than a
change here. Everything generic stays as module constants.

The scaling dials are deliberately NOT per-app: every dfe-infra chart routes KEDA
through the shared ``dfe-common.scaledobject`` helper, so one set of key paths covers
all of them. The app-repo charts use different names for the same dials, but Argo
deploys the dfe-infra family, so those names are the ones that reach a cluster.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from dfe_engine.yaml_utils import yaml_load

# dfe-infra chart key paths. Uniform across every chart because they all call the
# same KEDA library template - see helm/library/dfe-common/templates/_keda.tpl.
REPLICA_COUNT_PATH = "replicaCount"
CPU_REQUEST_PATH = "resources.requests.cpu"
MEMORY_REQUEST_PATH = "resources.requests.memory"
CPU_LIMIT_PATH = "resources.limits.cpu"
MEMORY_LIMIT_PATH = "resources.limits.memory"
KEDA_ENABLED_PATH = "keda.enabled"
KEDA_MIN_PATH = "keda.minReplicaCount"
KEDA_MAX_PATH = "keda.maxReplicaCount"

SCALING_PATHS = (
    REPLICA_COUNT_PATH,
    CPU_REQUEST_PATH,
    MEMORY_REQUEST_PATH,
    CPU_LIMIT_PATH,
    MEMORY_LIMIT_PATH,
    KEDA_ENABLED_PATH,
    KEDA_MIN_PATH,
    KEDA_MAX_PATH,
)

# The `deploy:` block the layer2-apps ApplicationSet reads off each values file to
# name the Argo Application. Without it the file produces no Application at all.
DEPLOY_SERVICE_PATH = "deploy.service"
DEPLOY_INSTANCE_PATH = "deploy.instance"

# Feeds dfe-common.fullname, so a per-config app carries its instance here to keep
# each deployment's Kubernetes object names distinct.
COMPONENT_PATH = "component"

# The chart's dial for the OTel service.name. scalo otherwise falls back to the
# app's binary name, leaving two instances of one app indistinguishable in the
# otel database. `env` is NOT the place for this: in the dfe-infra charts that key
# is a string (the deployment environment) feeding labels and the namespace, so a
# map there renders an invalid label value and every object is rejected.
OTEL_SERVICE_NAME_PATH = "otelServiceName"


class Multiplicity(StrEnum):
    """How many deployments of an app a stack runs.

    Independent of whether those deployments scale: a transform is per-config AND
    KEDA-scaled, since there may be hundreds of them, one per source, each
    replicating on its own load.
    """

    SINGLE = "single"
    """One deployment for the whole stack."""

    PER_CONFIG = "per_config"
    """One deployment per config, many side by side."""


class ReloadMode(StrEnum):
    """How a written change reaches the running process."""

    HOT = "hot"
    """The app watches the file and applies it without a restart."""

    ROLL = "roll"
    """A pod roll applies it - the chart's checksum annotation triggers one."""

    RESTART = "restart"
    """Needs a manual restart; nothing in the deploy path triggers one."""


@dataclass(frozen=True, slots=True)
class ConsumedFileSet:
    """One family of files an app reads off disk, and how we deliver them.

    Content is stored as a YAML block scalar under ``values_path`` in the instance's
    overlay, because Helm cannot read a raw file out of an Argo ``$values`` source -
    a chart-rendered ConfigMap can only contain what is already in the values.
    """

    name: str
    """Stable identifier for the set, used in the API path."""

    values_path: str
    """Dot-path in the overlay holding the filename -> content map."""

    dir_path: str
    """Dot-path of the app's own setting naming the directory it reads."""

    suffixes: tuple[str, ...]
    """Accepted file extensions. A name outside these is refused."""

    language: str
    """Editor hint for the UI - the syntax to highlight."""

    reload: ReloadMode


@dataclass(frozen=True, slots=True)
class AppDescriptor:
    """One deployable DFE app."""

    service: str
    """Chart and image name, e.g. ``dfe-transform-vrl``."""

    scale_deployed: bool
    """Whether the app carries the scaling dials at all."""

    multiplicity: Multiplicity
    """Whether the stack runs one deployment of this app, or one per config."""

    files: tuple[ConsumedFileSet, ...] = field(default_factory=tuple)

    source_binding: dict[str, object] = field(default_factory=dict)
    """Overlay dot-paths to set from the source name, ``{source}`` substituted."""

    @property
    def component_is_per_instance(self) -> bool:
        """Whether the chart's component name has to carry the instance.

        ``dfe-common.fullname`` is ``{project}-{component}`` with no instance, so
        every deployment of a per-config app would otherwise render identical
        Kubernetes object names and fight over them under Argo self-heal.
        """
        return self.multiplicity is Multiplicity.PER_CONFIG

    @property
    def source_bound(self) -> bool:
        """Whether an instance of this app IS a source's processing step."""
        return bool(self.source_binding)


class CatalogueError(ValueError):
    """Raised when the app manifest cannot be read or is malformed."""


BUNDLED_MANIFEST = Path(__file__).parent / "apps.yaml"
"""Snapshot shipped in the image, pinned to the dfe-infra manifest it came from."""


def _file_set_from(service: str, raw: dict) -> ConsumedFileSet:
    try:
        return ConsumedFileSet(
            name=str(raw["name"]),
            values_path=str(raw["values_path"]),
            dir_path=str(raw.get("dir_setting", "")),
            suffixes=tuple(str(s) for s in raw["suffixes"]),
            language=str(raw["language"]),
            reload=ReloadMode(str(raw.get("reload", ReloadMode.RESTART))),
        )
    except (KeyError, ValueError) as exc:
        raise CatalogueError(f"{service}: invalid file set {raw!r}: {exc}") from exc


def _descriptor_from(service: str, raw: dict) -> AppDescriptor:
    try:
        multiplicity = Multiplicity(str(raw.get("multiplicity", Multiplicity.SINGLE)))
    except ValueError as exc:
        raise CatalogueError(
            f"{service}: unknown multiplicity {raw.get('multiplicity')!r}"
        ) from exc
    binding = raw.get("source_binding") or {}
    if not isinstance(binding, dict):
        raise CatalogueError(f"{service}: source_binding must be a mapping")
    return AppDescriptor(
        service=service,
        scale_deployed=bool(raw.get("scale_deployed", True)),
        multiplicity=multiplicity,
        files=tuple(_file_set_from(service, f) for f in raw.get("files") or ()),
        source_binding=dict(binding),
    )


def load_catalogue(path: Path | str | None = None) -> dict[str, AppDescriptor]:
    """Read the app manifest into descriptors.

    The manifest is the source of truth for what apps exist and what kind of app
    each one is, so adding or changing an app is an edit there rather than a change
    here. Resolution order: the given path, then ``DFE_APP_CATALOGUE_FILE``, then the
    snapshot bundled in the image.
    """
    source = Path(path or os.getenv("DFE_APP_CATALOGUE_FILE") or BUNDLED_MANIFEST)
    if not source.is_file():
        raise CatalogueError(f"app manifest not found: {source}")
    try:
        doc = yaml_load(source) or {}
    except Exception as exc:
        raise CatalogueError(f"app manifest {source} is not readable: {exc}") from exc
    apps = doc.get("apps")
    if not isinstance(apps, dict) or not apps:
        raise CatalogueError(f"app manifest {source} declares no apps")
    return {name: _descriptor_from(name, raw or {}) for name, raw in apps.items()}


APP_CATALOGUE: dict[str, AppDescriptor] = load_catalogue()


def reload_catalogue(path: Path | str | None = None) -> dict[str, AppDescriptor]:
    """Re-read the manifest in place, so a remounted file takes effect."""
    APP_CATALOGUE.clear()
    APP_CATALOGUE.update(load_catalogue(path))
    return APP_CATALOGUE


class UnknownAppError(KeyError):
    """Raised when a service name is not in the catalogue."""


def descriptor(service: str) -> AppDescriptor:
    """Resolve a service name to its descriptor."""
    try:
        return APP_CATALOGUE[service]
    except KeyError:
        raise UnknownAppError(service) from None


def render_source_binding(app: AppDescriptor, source: str) -> dict[str, object]:
    """The overlay values that tie an instance of this app to ``source``."""

    def _fill(value: object) -> object:
        if isinstance(value, str):
            return value.format(source=source)
        if isinstance(value, list):
            return [_fill(v) for v in value]
        return value

    return {path: _fill(template) for path, template in app.source_binding.items()}


def file_set(service: str, name: str) -> ConsumedFileSet:
    """Resolve one of an app's consumed-file sets by name."""
    for candidate in descriptor(service).files:
        if candidate.name == name:
            return candidate
    raise UnknownAppError(f"{service} has no file set {name!r}")


def services() -> list[str]:
    """Every catalogued service name, sorted."""
    return sorted(APP_CATALOGUE)
