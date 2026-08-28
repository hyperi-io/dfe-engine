#  Project:      dfe-engine
#  File:         appmgmt/catalogue.py
#  Purpose:      The deployed-app catalogue - what the generic layer knows per app
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""What the app-management layer needs to know about each deployed DFE app.

Everything generic lives as module constants; the per-app table carries only what
genuinely differs - which files an app reads, and how a change to them takes effect.

The scaling dials are deliberately NOT per-app: every dfe-infra chart routes KEDA
through the shared ``dfe-common.scaledobject`` helper, so one set of key paths covers
all of them. The app-repo charts use different names for the same dials, but Argo
deploys the dfe-infra family, so those names are the ones that reach a cluster.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

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

# scalo resolves the OTel `service.name` resource attribute from OTEL_SERVICE_NAME
# first, then config, then the app's own binary name - so two instances of one app
# are indistinguishable in the otel database unless we set this per instance.
OTEL_SERVICE_NAME_PATH = "env.OTEL_SERVICE_NAME"


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

    multi_instance: bool
    """Whether more than one instance is a normal deployment."""

    files: tuple[ConsumedFileSet, ...] = field(default_factory=tuple)


_VRL_TRANSFORMS = ConsumedFileSet(
    name="transforms",
    values_path="transformFiles",
    dir_path="config.transforms.dir",
    suffixes=(".vrl",),
    language="vrl",
    # dfe-transform-vrl compiles every .vrl into one program at startup and holds it
    # immutable for the process lifetime, so only a pod roll applies an edit.
    reload=ReloadMode.ROLL,
)

_VECTOR_TRANSFORMS = ConsumedFileSet(
    name="transforms",
    values_path="transformFiles",
    dir_path="config.transforms.dir",
    suffixes=(".yaml", ".yml"),
    language="yaml",
    # dfe-transform-vector polls mtimes, re-assembles, runs `vector validate` and
    # SIGHUPs, rolling back if validation fails. Transform files are the only diff
    # it accepts hot; every other section is rejected as unsafe.
    reload=ReloadMode.HOT,
)


APP_CATALOGUE: dict[str, AppDescriptor] = {
    app.service: app
    for app in (
        AppDescriptor("dfe-loader", scale_deployed=True, multi_instance=False),
        AppDescriptor("dfe-receiver", scale_deployed=True, multi_instance=False),
        AppDescriptor("dfe-archiver", scale_deployed=True, multi_instance=False),
        AppDescriptor("dfe-fetcher", scale_deployed=True, multi_instance=True),
        AppDescriptor(
            "dfe-transform-vrl",
            scale_deployed=True,
            multi_instance=True,
            files=(_VRL_TRANSFORMS,),
        ),
        AppDescriptor(
            "dfe-transform-vector",
            scale_deployed=True,
            multi_instance=True,
            files=(_VECTOR_TRANSFORMS,),
        ),
        # dfe-transform-elastic selects a compiled-in transform by `source.name`;
        # it reads no user-authored files at all.
        AppDescriptor("dfe-transform-elastic", scale_deployed=True, multi_instance=True),
    )
}


class UnknownAppError(KeyError):
    """Raised when a service name is not in the catalogue."""


def descriptor(service: str) -> AppDescriptor:
    """Resolve a service name to its descriptor."""
    try:
        return APP_CATALOGUE[service]
    except KeyError:
        raise UnknownAppError(service) from None


def file_set(service: str, name: str) -> ConsumedFileSet:
    """Resolve one of an app's consumed-file sets by name."""
    for candidate in descriptor(service).files:
        if candidate.name == name:
            return candidate
    raise UnknownAppError(f"{service} has no file set {name!r}")


def services() -> list[str]:
    """Every catalogued service name, sorted."""
    return sorted(APP_CATALOGUE)
