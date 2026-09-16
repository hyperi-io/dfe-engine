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

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from dfe_engine.manifest import ManifestError, manifest_path, read_manifest
from dfe_engine.transport import TRANSPORTS

# Every DFE app's chart and image name starts with this, and every Kubernetes
# object it renders is ``dfe-<component>``.
PROJECT_PREFIX = "dfe-"

PUSH_ENDPOINT = "push"
"""The endpoint name a direct-transport stage sends to.

An app declares it in the manifest exactly when it runs a scalo Push listener, so
the declaration is also how the engine knows the app can be sent to at all. There
is deliberately no default port: a missing entry means no listener, which is a
different thing from a listener on the usual number.
"""

# The only two names ``mesh.host_pattern`` may substitute - the deployment's name
# for a pool, and where its listener lives - checked when the manifest loads.
MESH_INSTANCE = "instance"
MESH_NAMESPACE = "mesh_namespace"

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

    entries_path: str
    """Dot-path of the app's own setting holding one ``{name, path}`` entry per file.

    The other half of ``dir_path``: a set the app names table by table rather
    than by directory. Whoever owns the mount derives the entries, because only
    it knows the path - the chart on Kubernetes, the Compose writer off it.
    """

    suffixes: tuple[str, ...]
    """Accepted file extensions. A name outside these is refused."""

    language: str
    """Editor hint for the UI - the syntax to highlight."""

    reload: ReloadMode


@dataclass(frozen=True, slots=True)
class AppEndpoint:
    """One listener an app answers on, and where a sender addresses it."""

    port: int

    service: str = ""
    """Kubernetes Service, when the chart's is not named after the app itself."""


@dataclass(frozen=True, slots=True)
class CatalogueBinding:
    """An app that ships a catalogue of sources it already knows how to handle.

    The app owns the catalogue; this says which file carries it, where the
    entries sit inside that file, and how one entry names the compiled-in
    program it selects. All three are the app's own conventions, so an app with
    a differently shaped catalogue joins by being declared here rather than by a
    branch in the engine.
    """

    file: str
    """Filename the app publishes it under - what a mounted copy is checked against."""

    entries_key: str
    """Top-level key in that file holding the entries, one per source."""

    variant_pattern: str
    """How an entry becomes ``transform.variant``; ``{entry}`` and ``{transform}``."""

    def variant(self, entry: str, transform: str) -> str:
        """The compiled-in program an entry's named transform selects."""
        return self.variant_pattern.format(entry=entry, transform=transform)


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

    routing_paths: dict[str, str] = field(default_factory=dict)
    """Each derived block this app carries, by name, and its overlay dot-path.

    More than one because a block is the app's OWN config section: the receiver's
    routing rules and its destination set are siblings the app reads separately,
    and each is owned whole so switching transport removes the other's keys
    rather than leaving them beside the new ones.
    """

    routing_scope: RoutingScope = RoutingScope.STACK
    """Whether the block compiles from every source or from the bound one."""

    source_types: tuple[str, ...] = ()
    """The source families a source-bound instance of this app can poll."""

    catalogue_packages: dict[str, tuple[str, ...]] = field(default_factory=dict)
    """Family -> the catalogue packages it polls, where the two spell the vendor apart.

    An exact name match needs no entry, so this stays the exceptions list rather
    than a second copy of ``source_types``.
    """

    transports: frozenset[str] = frozenset({"bus"})
    """Which transports this app can carry a source's records on.

    Empty where the app carries no records at all, which is a different statement
    from carrying them on the bus: the door an appliance dials before it posts to
    the receiver is deployable and dialable, and no source ever names it.
    """

    profiles: frozenset[str] = frozenset()
    """The deployment profiles this app MAY be deployed in; empty means all.

    An offer the console lists, not a gate: a profile is a set of values in the
    cascade rather than a name a chart reads, so the deploy repo stays the only
    thing that decides what is deployed.
    """

    default_in: frozenset[str] | None = None
    """The profiles a deployment runs this app in WITHOUT being asked.

    None means the same as ``profiles``, so an app that says nothing about
    either is deployed everywhere. An empty set is the other end of the same
    scale: nothing deploys it, and an operator turns it on.
    """

    idle_when: tuple[str, ...] = ()
    """The config dot-paths whose emptiness means this app has no work.

    Reported, never evaluated: the apps carry the same predicate in their own
    ``work_state``, and this is the declaration a console reads to say why one
    of them is sitting idle. Empty means the app always has work.
    """

    hot_reload: bool = False
    """Whether the app can apply a config change in place; reported, never acted on.

    Every chart checksums its whole config into the pod template, so a change
    rolls the pods as a rolling update whether or not the app could reload.
    """

    endpoints: dict[str, AppEndpoint] = field(default_factory=dict)
    """The listeners this app runs, by name. Absent means the app has none."""

    variant_path: str = ""
    """This app's own config key naming the compiled-in program an instance runs.

    Declared per app so the engine writes a source's ``transform.variant`` into
    whatever the app calls it, without knowing which app it is.
    """

    catalogue: CatalogueBinding | None = None
    """The catalogue of sources this app ships, when it ships one."""

    config_file: str = ""
    """The config file this app's own image reads, by name; empty where it reads none.

    Declared because it is the app's fact, not the deployment's: the image's own
    CMD spells it. On Kubernetes the chart mounts the rendered ConfigMap under
    that name; off it, the Compose writer renders the same content there.
    """

    def carries(self, transport: str) -> bool:
        """Whether this app can carry a source on *transport*."""
        return transport in self.transports

    def offered_in(self, profile: str) -> bool:
        """Whether this deployment profile may deploy this app.

        An app naming no profiles may be deployed in all of them, and a caller
        that knows no profile is told the same, so an unset deployment fact
        lists everything rather than silently hiding the optional apps.
        """
        return not self.profiles or not profile or profile in self.profiles

    @property
    def optional(self) -> bool:
        """Whether a deployment runs without this app.

        Derived rather than declared: an app nothing deploys by default IS the
        optional one, so the two facts cannot drift apart in the manifest.
        """
        return self.default_in is not None and not self.default_in

    def block_for(self, path: str) -> tuple[str, str]:
        """The derived block a compiled dot-path belongs to, and the path within it.

        The manifest declares which parts of an overlay the engine owns, so a
        compiled value outside all of them is a manifest and compiler that
        disagree - caught here rather than by a key silently going nowhere.
        """
        for name, root in self.routing_paths.items():
            if path == root:
                return name, ""
            if path.startswith(f"{root}."):
                return name, path[len(root) + 1 :]
        declared = ", ".join(sorted(self.routing_paths.values())) or "none"
        raise CatalogueError(
            f"{self.service}: {path!r} is outside every derived block (declared: {declared})"
        )

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
        return bool(self.routing_compiler and self.routing_paths)

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

    @property
    def transform_engine(self) -> str:
        """The engine a source names to select this app, or "" when it is not one.

        A transform app is source-bound and named for the engine it runs, so the
        name is derived here once: ``transform_engines`` is the set of these and
        the API reports this field, which is why a console picker and the write
        path cannot disagree about what a transform app is.
        """
        if not (self.source_bound and self.service.startswith(TRANSFORM_SERVICE_PREFIX)):
            return ""
        return self.service.removeprefix(TRANSFORM_SERVICE_PREFIX)


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


class CatalogueError(ManifestError):
    """Raised when the app manifest cannot be read or is malformed."""


class MissingEndpointError(CatalogueError):
    """Raised when a stage needs an app's listener and the manifest declares none."""


BUNDLED_MANIFEST = Path(__file__).parent / "apps.yaml"
"""Snapshot shipped in the image, pinned to the dfe-infra manifest it came from."""


def _file_set_from(service: str, raw: dict) -> ConsumedFileSet:
    try:
        values_path = str(raw["values_path"])
        file_set = ConsumedFileSet(
            name=str(raw["name"]),
            values_path=values_path,
            links_path=str(raw.get("links_path") or f"{values_path}Links"),
            dir_path=str(raw.get("dir_setting", "")),
            entries_path=str(raw.get("entries_path", "")),
            suffixes=tuple(str(s) for s in raw["suffixes"]),
            language=str(raw["language"]),
            reload=ReloadMode(str(raw.get("reload", ReloadMode.RESTART))),
        )
    except (KeyError, ValueError) as exc:
        raise CatalogueError(f"{service}: invalid file set {raw!r}: {exc}") from exc
    # Both would have the renderer name the same files twice, and the app read
    # one of the two answers depending on which key it happens to consult.
    if file_set.dir_path and file_set.entries_path:
        raise CatalogueError(
            f"{service}: file set {file_set.name!r} declares both dir_setting and "
            "entries_path; an app reads a set as a directory or entry by entry, not both"
        )
    return file_set


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
    families = tuple(str(t) for t in types)
    app = AppDescriptor(
        service=service,
        scale_deployed=bool(raw.get("scale_deployed", True)),
        multiplicity=multiplicity,
        files=tuple(_file_set_from(service, f) for f in raw.get("files") or ()),
        source_binding=dict(binding),
        routing_compiler=str(routing.get("compiler", "")),
        routing_paths=_routing_paths_from(service, routing.get("values_paths")),
        routing_scope=scope,
        source_types=families,
        catalogue_packages=_catalogue_packages_from(
            service, raw.get("catalogue_packages"), families
        ),
        transports=_transports_from(service, raw.get("transports")),
        profiles=_profiles_from(service, "profiles", raw.get("profiles")),
        default_in=_default_in_from(service, raw.get("default_in")),
        idle_when=_idle_when_from(service, raw.get("idle_when")),
        hot_reload=bool(raw.get("hot_reload", False)),
        endpoints=_endpoints_from(service, raw.get("endpoints")),
        variant_path=str(raw.get("variant_path", "")),
        catalogue=_catalogue_from(service, raw.get("catalogue")),
        config_file=_config_file_from(service, raw.get("consumes")),
    )
    # The variant is written into one of the derived blocks, so a path outside
    # them would be compiled and then dropped on the next sync.
    if app.variant_path:
        app.block_for(app.variant_path)
    return app


def _config_file_from(service: str, raw: object) -> str:
    """The config file name this app's image reads, empty when it reads none.

    A plain basename, because whoever renders it decides the directory: the
    chart's mount on Kubernetes, the writer's output directory off it. A path
    here would put the file somewhere neither of them mounts.
    """
    if raw is None:
        return ""
    if not isinstance(raw, dict):
        raise CatalogueError(f"{service}: consumes must be a mapping")
    name = str(raw.get("config", ""))
    if not name:
        raise CatalogueError(f"{service}: consumes needs a config file name")
    if "/" in name or name in (".", ".."):
        raise CatalogueError(
            f"{service}: consumes.config is {name!r}; it must be a plain file name, "
            "because the directory belongs to whoever mounts it"
        )
    return name


def _routing_paths_from(service: str, raw: object) -> dict[str, str]:
    """The overlay dot-path of each block this app's compiler produces."""
    if raw is None:
        return {}
    if not isinstance(raw, dict) or not raw:
        raise CatalogueError(f"{service}: routing.values_paths must be a non-empty mapping")
    return {str(name): str(path) for name, path in raw.items()}


def _catalogue_packages_from(
    service: str, raw: object, families: tuple[str, ...]
) -> dict[str, tuple[str, ...]]:
    """Which catalogue packages each of this app's families polls.

    Every key has to be one of the app's own ``source_types``: a family it does
    not have cannot poll anything, and a typo there would silently offer a
    catalogue entry a fetcher family that does not exist.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise CatalogueError(
            f"{service}: catalogue_packages must be a mapping of family to packages"
        )
    out: dict[str, tuple[str, ...]] = {}
    for family, packages in raw.items():
        if str(family) not in families:
            raise CatalogueError(
                f"{service}: catalogue_packages names {family!r}, which is not one of its "
                f"source_types ({', '.join(families) or 'none'})"
            )
        if not isinstance(packages, list) or not packages:
            raise CatalogueError(
                f"{service}: catalogue_packages[{family!r}] must be a non-empty list of packages"
            )
        out[str(family)] = tuple(str(p) for p in packages)
    return out


def _catalogue_from(service: str, raw: object) -> CatalogueBinding | None:
    """The catalogue this app ships, or None when it ships none."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise CatalogueError(f"{service}: catalogue must be a mapping")
    try:
        binding = CatalogueBinding(
            file=str(raw["file"]),
            entries_key=str(raw["entries_key"]),
            variant_pattern=str(raw["variant_pattern"]),
        )
    except KeyError as exc:
        raise CatalogueError(
            f"{service}: catalogue needs file, entries_key and variant_pattern; missing {exc}"
        ) from exc
    # Rendered here rather than at the first use, so a pattern naming a
    # placeholder the engine never substitutes is a manifest error, not a
    # transform instance told to run a program with a brace in its name.
    try:
        binding.variant("entry", "transform")
    except (KeyError, IndexError) as exc:
        raise CatalogueError(
            f"{service}: catalogue.variant_pattern {binding.variant_pattern!r} takes only "
            f"{{entry}} and {{transform}}: {exc}"
        ) from exc
    return binding


def _transports_from(service: str, raw: object) -> frozenset[str]:
    """The transports an app declares, defaulting to the bus alone.

    Refusing an unknown name here is what makes the model's refusals trustworthy:
    a typo would otherwise read as "this app cannot do direct" and reject sources
    for a reason nobody could see. An explicit empty list is a statement in its
    own right - this app carries no records - and is kept, because defaulting it
    to the bus would claim a data path the app does not have.
    """
    if raw is None:
        return frozenset({"bus"})
    # An empty list is an app that carries no records at all, such as the VPN.
    if not isinstance(raw, list):
        raise CatalogueError(f"{service}: transports must be a list")
    declared = {str(t) for t in raw}
    unknown = declared - TRANSPORTS
    if unknown:
        raise CatalogueError(
            f"{service}: unknown transport(s) {', '.join(sorted(unknown))}; "
            f"valid: {', '.join(sorted(TRANSPORTS))}"
        )
    return frozenset(declared)


def _profile_names(service: str, key: str, raw: object) -> frozenset[str]:
    """One manifest key's list of profile names."""
    if not isinstance(raw, list):
        raise CatalogueError(f"{service}: {key} must be a list of profile names")
    return frozenset(str(p) for p in raw)


def _profiles_from(service: str, key: str, raw: object) -> frozenset[str]:
    """Where an app MAY be deployed, empty meaning every profile.

    An empty list is refused rather than read as "all": a list written and left
    empty says the app may be deployed nowhere, which no manifest means, and the
    absent key already says "everywhere".
    """
    if raw is None:
        return frozenset()
    names = _profile_names(service, key, raw)
    if not names:
        raise CatalogueError(
            f"{service}: {key} must be a non-empty list; omit the key for every profile"
        )
    return names


def _default_in_from(service: str, raw: object) -> frozenset[str] | None:
    """Where an app is deployed WITHOUT being asked, None meaning wherever it may be.

    An empty list is the meaningful case here rather than a mistake: it is how a
    manifest says nothing deploys this app until an operator turns it on, which
    is what makes the app optional.
    """
    if raw is None:
        return None
    return _profile_names(service, "default_in", raw)


def _idle_when_from(service: str, raw: object) -> tuple[str, ...]:
    """The config dot-paths whose emptiness means the app has no work.

    Read and reported only - the apps evaluate the same predicate themselves, so
    the engine never resolves these paths against an overlay.
    """
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise CatalogueError(f"{service}: idle_when must be a list of config dot-paths")
    return tuple(str(p) for p in raw)


def _endpoints_from(service: str, raw: object) -> dict[str, AppEndpoint]:
    """The listeners an app runs, empty when it runs none."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise CatalogueError(f"{service}: endpoints must be a mapping of name to listener")
    out: dict[str, AppEndpoint] = {}
    for name, entry in raw.items():
        if not isinstance(entry, dict):
            raise CatalogueError(f"{service}: endpoint {name!r} must be a mapping with a port")
        try:
            out[str(name)] = AppEndpoint(
                port=int(entry["port"]), service=str(entry.get("service", ""))
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CatalogueError(
                f"{service}: endpoint {name!r} needs an integer port: {exc}"
            ) from exc
    return out


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
    snapshot bundled in the image. The snapshot is always there, so the resolver
    always names a file.
    """
    source = manifest_path(path, "DFE_APP_CATALOGUE_FILE", BUNDLED_MANIFEST)
    if source is None:
        raise CatalogueError("app manifest not found: nothing names one")
    return read_manifest(source, what="app manifest")


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


def load_mesh(path: Path | str | None = None) -> str:
    """The address SHAPE a sender uses where the stage pools sit behind a listener.

    Declared once for the whole manifest rather than per endpoint: the charts
    render one listener alias per pool from the same rule, so a copy under each
    app would be a second thing to keep in step. Empty when the manifest declares
    none, which is every deployment whose senders dial the pools' own Services.
    """
    mesh = _read_manifest(path).get("mesh") or {}
    if not isinstance(mesh, dict):
        raise CatalogueError("app manifest 'mesh' must be a mapping")
    pattern = str(mesh.get("host_pattern") or "")
    if pattern:
        try:
            pattern.format(**{MESH_INSTANCE: "", MESH_NAMESPACE: ""})
        except (KeyError, IndexError) as exc:
            raise CatalogueError(
                f"mesh.host_pattern may name only {{{MESH_INSTANCE}}} and "
                f"{{{MESH_NAMESPACE}}}: {exc}"
            ) from exc
    return pattern


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

MESH_HOST_PATTERN: str = load_mesh()


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


def _push(app: AppDescriptor) -> AppEndpoint:
    """This app's Push listener, or why it cannot be sent to.

    Refusing rather than assuming a port is the point: an app with no entry has
    no listener, and inventing an address for it would send a source's records
    at a port nothing answers on.
    """
    endpoint = app.endpoints.get(PUSH_ENDPOINT)
    if endpoint is None:
        raise MissingEndpointError(
            f"{app.service} declares no {PUSH_ENDPOINT!r} endpoint, so nothing can send "
            "to it on the direct transport; add one to the app manifest when it ships "
            "a listener"
        )
    return endpoint


def _deployed_name(app: AppDescriptor, instance: str) -> str:
    """The name this app's own chart renders the deployment under.

    A stack-wide app is named for itself; a per-config app for its instance.
    """
    return instance_name(app, instance) if app.component_is_per_instance else app.service


def _mesh_host(app: AppDescriptor, instance: str, namespace: str) -> str:
    """The listener alias a sender dials to reach this deployment's pool.

    Built from the deployment's own name and never from an endpoint's ``service``
    override: the alias is rendered beside the pool by its own chart, off the
    same name.
    """
    if not MESH_HOST_PATTERN:
        raise CatalogueError(
            "this deployment balances its pools behind listeners, but the app manifest "
            "declares no mesh.host_pattern to address them by"
        )
    return MESH_HOST_PATTERN.format(
        **{MESH_INSTANCE: _deployed_name(app, instance), MESH_NAMESPACE: namespace}
    )


def push_endpoint(app: AppDescriptor, instance: str, mesh_namespace: str = "") -> str:
    """Where a direct-transport stage sends records for this app's *instance*.

    A stack-wide app answers on its own Service; a per-config app answers on the
    instance's. Both the port and any Service-name override are the manifest's,
    so a chart that moves its listener is a manifest edit.

    Given a namespace, the pool sits behind a listener there and the sender dials
    that alias on the same port instead: a Service balances per connection and
    gRPC holds one, so every record would otherwise go to the pod the first
    connection landed on.
    """
    endpoint = _push(app)
    if mesh_namespace:
        return f"http://{_mesh_host(app, instance, mesh_namespace)}:{endpoint.port}"
    return f"http://{endpoint.service or _deployed_name(app, instance)}:{endpoint.port}"


def push_listen(app: AppDescriptor) -> str:
    """The bind address this app's own Push listener takes.

    The sender's view of the same manifest entry is ``push_endpoint``: one
    declared port, rendered as an address to dial and an address to bind. A pod
    binds every interface it has, so only the port comes from the manifest.
    """
    return f"0.0.0.0:{_push(app).port}"


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


def source_type_for_package(package: str) -> str | None:
    """The fetcher family that polls a catalogue package, or None when none does.

    A catalogue names its packages the way its own vendor does, and the fetcher
    names its families the way ITS vendor does; the two agree most of the time
    and the manifest carries the handful of places they do not. An unmapped
    package is a real answer: the fetcher cannot poll that source.
    """
    for app in APP_CATALOGUE.values():
        if package in app.source_types:
            return package
        for family, packages in app.catalogue_packages.items():
            if package in packages:
                return family
    return None


def catalogue_app(filename: str) -> AppDescriptor:
    """The app whose catalogue a mounted file carries.

    Matched on the filename the app declares, so the transform that owns a
    mounted catalogue is a fact in the manifest rather than an assumption about
    which app is the only one with a catalogue today.
    """
    declared = {app.catalogue.file: app for app in APP_CATALOGUE.values() if app.catalogue}
    try:
        return declared[filename]
    except KeyError:
        raise CatalogueError(
            f"no catalogued app ships a source catalogue named {filename!r} "
            f"(declared: {', '.join(sorted(declared)) or 'none'})"
        ) from None


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
    return {app.transform_engine for app in APP_CATALOGUE.values() if app.transform_engine}
