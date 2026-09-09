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

from dfe_engine.transport import TRANSPORTS
from dfe_engine.yaml_utils import yaml_load

# Every DFE app's chart and image name starts with this, and every Kubernetes
# object it renders is ``dfe-<component>``.
PROJECT_PREFIX = "dfe-"

# The gRPC port scalo's Push service listens on. One number for every app that
# has a listener; a manifest ``endpoints`` entry overrides it per app.
DEFAULT_PUSH_PORT = 6000

PUSH_ENDPOINT = "push"
"""The endpoint name a direct-transport stage sends to."""

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


class RoutingScope(StrEnum):
    """What a compiled routing block is derived from."""

    STACK = "stack"
    """Every source: one block for the whole stack, on a single-deployment app."""

    INSTANCE = "instance"
    """The one source the instance is bound to; the engine deploys such instances."""


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

    links_path: str
    """Dot-path in the overlay holding the library links that resolved into the set."""

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

    routing_compiler: str = ""
    """Name of the compiler that derives this app's routing from the sources."""

    routing_path: str = ""
    """Overlay dot-path the compiled routing is written to."""

    routing_scope: RoutingScope = RoutingScope.STACK
    """Whether the block compiles from every source or from the bound one."""

    source_types: tuple[str, ...] = ()
    """The source families a source-bound instance of this app can poll."""

    transports: frozenset[str] = frozenset({"bus"})
    """Which transports this app can carry a source's records on."""

    hot_reload: bool = False
    """Whether the app can apply a config change in place; reported, never acted on.

    Every chart checksums its whole config into the pod template, so a change
    rolls the pods as a rolling update whether or not the app could reload.
    """

    endpoints: dict[str, int] = field(default_factory=dict)
    """Named listener ports, where the app deviates from the platform default."""

    def carries(self, transport: str) -> bool:
        """Whether this app can carry a source on *transport*."""
        return transport in self.transports

    @property
    def routing_is_per_instance(self) -> bool:
        """Whether each instance's routing comes from its own source.

        Such instances are derived state: the engine deploys one per active
        source of the matching origin and removes it when the source goes.
        """
        return self.has_compiled_routing and self.routing_scope is RoutingScope.INSTANCE

    @property
    def has_compiled_routing(self) -> bool:
        """Whether this app's routing is derived from the source definitions.

        A derived block is not hand-editable: it is recompiled from the sources,
        so the API reports drift against them rather than treating an edit as
        intent.
        """
        return bool(self.routing_compiler and self.routing_path)

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


class Encoding(StrEnum):
    """How an artefact's content travels through the API and sits in the overlay."""

    TEXT = "text"
    """Stored and transported verbatim."""

    BASE64 = "base64"
    """Transported base64-encoded; the digest is taken over the decoded bytes."""


@dataclass(frozen=True, slots=True)
class ArtifactKind:
    """One kind of thing the versioned library can hold."""

    name: str
    """Identifier used in the API and recorded on every version."""

    language: str
    """Editor hint, and the key a syntax validator registers under."""

    suffixes: tuple[str, ...]
    """Extensions a file of this kind may carry."""

    encoding: Encoding


class CatalogueError(ValueError):
    """Raised when the app manifest cannot be read or is malformed."""


BUNDLED_MANIFEST = Path(__file__).parent / "apps.yaml"
"""Snapshot shipped in the image, pinned to the dfe-infra manifest it came from."""


def _file_set_from(service: str, raw: dict) -> ConsumedFileSet:
    try:
        values_path = str(raw["values_path"])
        return ConsumedFileSet(
            name=str(raw["name"]),
            values_path=values_path,
            links_path=str(raw.get("links_path") or f"{values_path}Links"),
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
    routing = raw.get("routing") or {}
    if not isinstance(routing, dict):
        raise CatalogueError(f"{service}: routing must be a mapping")
    try:
        scope = RoutingScope(str(routing.get("scope", RoutingScope.STACK)))
    except ValueError as exc:
        raise CatalogueError(f"{service}: unknown routing scope {routing.get('scope')!r}") from exc
    if scope is RoutingScope.INSTANCE and multiplicity is not Multiplicity.PER_CONFIG:
        raise CatalogueError(f"{service}: instance-scoped routing needs multiplicity per_config")
    types = raw.get("source_types") or []
    if not isinstance(types, list):
        raise CatalogueError(f"{service}: source_types must be a list")
    return AppDescriptor(
        service=service,
        scale_deployed=bool(raw.get("scale_deployed", True)),
        multiplicity=multiplicity,
        files=tuple(_file_set_from(service, f) for f in raw.get("files") or ()),
        source_binding=dict(binding),
        routing_compiler=str(routing.get("compiler", "")),
        routing_path=str(routing.get("values_path", "")),
        routing_scope=scope,
        source_types=tuple(str(t) for t in types),
        transports=_transports_from(service, raw.get("transports")),
        hot_reload=bool(raw.get("hot_reload", False)),
        endpoints=_endpoints_from(service, raw.get("endpoints")),
    )


def _transports_from(service: str, raw: object) -> frozenset[str]:
    """The transports an app declares, defaulting to the bus alone.

    Refusing an unknown name here is what makes the model's refusals trustworthy:
    a typo would otherwise read as "this app cannot do direct" and reject sources
    for a reason nobody could see.
    """
    if raw is None:
        return frozenset({"bus"})
    if not isinstance(raw, list) or not raw:
        raise CatalogueError(f"{service}: transports must be a non-empty list")
    declared = {str(t) for t in raw}
    unknown = declared - TRANSPORTS
    if unknown:
        raise CatalogueError(
            f"{service}: unknown transport(s) {', '.join(sorted(unknown))}; "
            f"valid: {', '.join(sorted(TRANSPORTS))}"
        )
    return frozenset(declared)


def _endpoints_from(service: str, raw: object) -> dict[str, int]:
    """An app's named listener ports, empty when it takes the platform defaults."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise CatalogueError(f"{service}: endpoints must be a mapping of name to port")
    try:
        return {str(name): int(port) for name, port in raw.items()}
    except (TypeError, ValueError) as exc:
        raise CatalogueError(f"{service}: endpoint ports must be integers: {exc}") from exc


def _kind_from(name: str, raw: dict) -> ArtifactKind:
    try:
        return ArtifactKind(
            name=name,
            language=str(raw["language"]),
            suffixes=tuple(str(s) for s in raw["suffixes"]),
            encoding=Encoding(str(raw.get("encoding", Encoding.TEXT))),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise CatalogueError(f"invalid kind {name!r}: {exc}") from exc


def _read_manifest(path: Path | str | None) -> dict:
    """Load the manifest document.

    Resolution order: the given path, then ``DFE_APP_CATALOGUE_FILE``, then the
    snapshot bundled in the image.
    """
    source = Path(path or os.getenv("DFE_APP_CATALOGUE_FILE") or BUNDLED_MANIFEST)
    if not source.is_file():
        raise CatalogueError(f"app manifest not found: {source}")
    try:
        doc = yaml_load(source) or {}
    except Exception as exc:
        raise CatalogueError(f"app manifest {source} is not readable: {exc}") from exc
    if not isinstance(doc, dict):
        raise CatalogueError(f"app manifest {source} is not a mapping")
    return doc


def load_catalogue(path: Path | str | None = None) -> dict[str, AppDescriptor]:
    """Read the app manifest into descriptors.

    The manifest is the source of truth for what apps exist and what kind of app
    each one is, so adding or changing an app is an edit there rather than a change
    here.
    """
    doc = _read_manifest(path)
    apps = doc.get("apps")
    if not isinstance(apps, dict) or not apps:
        raise CatalogueError("app manifest declares no apps")
    return {name: _descriptor_from(name, raw or {}) for name, raw in apps.items()}


def load_kinds(path: Path | str | None = None) -> dict[str, ArtifactKind]:
    """Read the artefact kinds the versioned library accepts.

    Declared in the same manifest as the apps, so supporting a new authored
    language is an edit there. A manifest with no ``kinds`` block yields none, and
    the library then refuses every publish rather than guessing a kind.
    """
    kinds = _read_manifest(path).get("kinds") or {}
    if not isinstance(kinds, dict):
        raise CatalogueError("app manifest 'kinds' must be a mapping")
    return {name: _kind_from(name, raw or {}) for name, raw in kinds.items()}


APP_CATALOGUE: dict[str, AppDescriptor] = load_catalogue()

ARTIFACT_KINDS: dict[str, ArtifactKind] = load_kinds()


def reload_catalogue(path: Path | str | None = None) -> dict[str, AppDescriptor]:
    """Re-read the manifest in place, so a remounted file takes effect."""
    APP_CATALOGUE.clear()
    APP_CATALOGUE.update(load_catalogue(path))
    ARTIFACT_KINDS.clear()
    ARTIFACT_KINDS.update(load_kinds(path))
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


def instance_name(app: AppDescriptor, source: str) -> str:
    """The deployed name of this app's instance for *source*.

    The one place the ``dfe-<component>-<source>`` convention lives. It is the
    Argo Application name, the OTel ``service.name``, and the stem of every
    Kubernetes object the instance's chart renders, so all three move together.
    """
    return f"{app.service}-{source}"


def instance_component(app: AppDescriptor, source: str) -> str:
    """The chart ``component`` for that instance - the name without the project prefix.

    ``dfe-common.fullname`` prepends the project again, so handing it the full
    name would render ``dfe-dfe-transform-vrl-auth``.
    """
    return instance_name(app, source).removeprefix(PROJECT_PREFIX)


def push_endpoint(app: AppDescriptor, instance: str) -> str:
    """Where a direct-transport stage sends records for this app's *instance*.

    A stack-wide app answers on its own Service; a per-config app answers on the
    instance's. The port is the manifest's when the app declares one, else the
    platform default - so a chart that moves its listener is a manifest edit.
    """
    host = instance_name(app, instance) if app.component_is_per_instance else app.service
    port = app.endpoints.get(PUSH_ENDPOINT, DEFAULT_PUSH_PORT)
    return f"http://{host}:{port}"


def file_set(service: str, name: str) -> ConsumedFileSet:
    """Resolve one of an app's consumed-file sets by name."""
    for candidate in descriptor(service).files:
        if candidate.name == name:
            return candidate
    raise UnknownAppError(f"{service} has no file set {name!r}")


def services() -> list[str]:
    """Every catalogued service name, sorted."""
    return sorted(APP_CATALOGUE)


TRANSFORM_SERVICE_PREFIX = "dfe-transform-"
"""A transform app's service name is this prefix plus the engine a source names."""


def source_types() -> set[str]:
    """Every source family a fetcher-based source may name in ``fetcher.source_type``.

    Declared per app in the manifest, so a fetcher that ships a new family is a
    manifest edit rather than an engine release.
    """
    return {t for app in APP_CATALOGUE.values() for t in app.source_types}


def instance_routed_apps() -> list[AppDescriptor]:
    """The apps whose instances the engine derives one-per-source from the routing."""
    return [app for app in APP_CATALOGUE.values() if app.routing_is_per_instance]


def transform_service(engine: str) -> str:
    """The catalogued app name a source's ``transform.engine`` selects."""
    return f"{TRANSFORM_SERVICE_PREFIX}{engine}"


def transform_engines() -> set[str]:
    """Every engine a source may name in ``transform.engine``.

    A transform app is a source-bound app, so the set is the catalogue's, and an
    engine it does not list has nothing to deploy.
    """
    return {
        service.removeprefix(TRANSFORM_SERVICE_PREFIX)
        for service, app in APP_CATALOGUE.items()
        if app.source_bound and service.startswith(TRANSFORM_SERVICE_PREFIX)
    }
