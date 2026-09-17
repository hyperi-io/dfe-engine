#  Project:      dfe-engine
#  File:         schema/plan.py
#  Purpose:      Render the dfe-schemas apply manifest into this deployment's plan
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Turn the pinned dfe-schemas manifest into the objects this deployment applies.

The manifest is the one list, and this is the only place the engine reads it.
Every object is rendered for the topology sensed off the live server, so the same
definition becomes a plain ``MergeTree`` on a standalone node, a
``ReplicatedMergeTree`` against a Replicated database, and a ``ReplicatedMergeTree
ON CLUSTER`` against an Atomic database on a real cluster.

Two trees can contribute. The pinned wheel inside the image is the core and is
read-only. A deployment's own overlay directory comes second and is ADDITIVE: it
may declare objects the core does not, and an object of its own whose definition
path sits under a core directory is refused by name rather than applied, so a
deployment cannot redefine a core table by shadowing its file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from dfe_schemas import __version__ as schemas_version
from dfe_schemas import schemas_root
from dfe_schemas.clickhouse import Topology
from dfe_schemas.manifest import Manifest, ManifestError, ManifestObject, load_manifest
from dfe_schemas.render import RenderedObject, Renderer
from scalo.logger import logger

from dfe_engine.schema.engine_resolver import EngineResolver, parse_engine

if TYPE_CHECKING:
    from dfe_engine.settings import DFESettings

# Where a deployment's own overlay tree is mounted. Absent is the normal case --
# a docker install with no deploy repo boots core-only.
OVERLAY_DIR_ENV_VAR = "DFE_SCHEMAS_OVERLAY_DIR"

# Directories the core owns. An overlay object defining a file under one of these
# is refused: the core definition is the one the rest of the suite is built on.
CORE_DIRS = (
    "common-header",
    "registries",
    "roles",
    "tables/core",
    "tables/internal",
    "tables/meta",
    "tables/otel",
    "topics",
    "views",
)

# The manifest ids of the two objects the applier needs before it can record
# anything, its own ledger included. Names are never written here -- the
# rendered object carries whatever the definition declares.
LEDGER_ID = "data.schema_migrations"
LOCK_ID = "data.schema_lock"


class SchemaPlanError(Exception):
    """The manifest could not be read, or the schema tree could not be resolved."""


@dataclass(frozen=True)
class SchemaPlan:
    """Every object this deployment applies, rendered and checksummed."""

    objects: tuple[RenderedObject, ...]
    schemas_version: str
    topology: str
    data_database: str
    # Overlay objects refused for redefining a core path, as "<id>: <reason>".
    refused: tuple[str, ...] = ()
    overlay_root: str = ""

    def by_id(self, object_id: str) -> RenderedObject:
        """One rendered object, by its manifest id."""
        for rendered in self.objects:
            if rendered.id == object_id:
                return rendered
        raise SchemaPlanError(f"the manifest declares no object {object_id!r}")

    def tables(self) -> tuple[RenderedObject, ...]:
        """Every ClickHouse object, in manifest order. Topics are not among them."""
        return tuple(obj for obj in self.objects if obj.kind != "topic")

    def topics(self) -> tuple[RenderedObject, ...]:
        """Every Kafka topic the bootstrap set declares."""
        return tuple(obj for obj in self.objects if obj.kind == "topic")


def overlay_root() -> Path | None:
    """The deployment's overlay tree, or None when it carries none.

    Optional by design: the absence of a deploy repo is a core-only boot, not a
    failure.
    """
    declared = os.getenv(OVERLAY_DIR_ENV_VAR, "").strip()
    if not declared:
        return None
    path = Path(declared)
    return path if path.is_dir() else None


@lru_cache(maxsize=8)
def _manifest(root: Path) -> Manifest:
    """The checked manifest for one tree, read once per root.

    Cached because the read paths ask for a name on every call -- the repository
    store, the alert cooldown and the hunt runner each assert their tables -- and
    re-reading and re-checking 54 objects for one lookup is the whole cost.
    """
    try:
        return load_manifest(root=root)
    except ManifestError as exc:
        raise SchemaPlanError(f"the dfe-schemas apply manifest is unusable: {exc}") from exc


def _redefines_core(obj: ManifestObject) -> bool:
    """Whether an overlay object's definition sits under a directory the core owns."""
    defines = (obj.defines or "").strip("/")
    return any(defines == core or defines.startswith(f"{core}/") for core in CORE_DIRS)


def sense_topology(
    client: Any | None, database: str, *, topology_setting: str | None = None
) -> tuple[Topology, str]:
    """The topology to render for, and the cluster to fan DDL over.

    Sensed off the live server through the same resolver every other DDL path
    uses, so the manifest render and the per-source render cannot disagree about
    what this deployment is.
    """
    resolved = EngineResolver(client=client, topology_setting=topology_setting).resolve(
        parse_engine("MergeTree"), database
    )
    cluster = resolved.on_cluster.strip().removeprefix("ON CLUSTER ").strip()
    if cluster:
        return Topology.REPLICATED_ON_CLUSTER, cluster
    if resolved.topology == "replicated":
        return Topology.REPLICATED, ""
    return Topology.SINGLE, ""


def _renderer(
    manifest: Manifest,
    *,
    topology: Topology,
    data_database: str,
    default_ttl_days: int | None,
    cluster: str,
    broker_count: int,
    kafka_tiered_storage: bool = False,
) -> Renderer:
    return Renderer(
        manifest,
        topology=topology,
        data_database=data_database,
        default_ttl_days=default_ttl_days,
        cluster=cluster or "dfe_cluster",
        broker_count=broker_count,
        kafka_tiered_storage=kafka_tiered_storage,
    )


def _render_all(
    renderer: Renderer, objects: tuple[ManifestObject, ...], *, source: str
) -> list[RenderedObject]:
    rendered: list[RenderedObject] = []
    for obj in objects:
        try:
            rendered.append(renderer.render(obj))
        except Exception as exc:
            raise SchemaPlanError(f"{source} object {obj.id!r} did not render: {exc}") from exc
    return rendered


def build_plan(
    *,
    settings: DFESettings,
    client: Any | None = None,
    broker_count: int = 1,
    kafka_tiered_storage: bool = False,
) -> SchemaPlan:
    """Read the manifest, sense the topology, render every object.

    Args:
        settings: The deployment's settings; the data database, the default
            retention and the topology fallback come from ``settings.clickhouse``.
        client: A live ClickHouse client to sense the topology on. None renders
            for the configured fallback, which never emits ``ON CLUSTER``.
        broker_count: Brokers the deployment has, so the topic set's replication
            factor is clamped to what the bus can actually place. Ask the bus for
            this; the configured factor clamped against itself never reduces.
        kafka_tiered_storage: Whether this deployment's brokers tier to object
            storage, which puts ``remote.storage.enable`` on the landing topic.

    Raises:
        SchemaPlanError: The manifest is missing or malformed, or an object in it
            could not be rendered.
    """
    ch = settings.clickhouse
    database = ch.effective_data_database
    try:
        manifest = _manifest(core_schemas_root())
    except ManifestError as exc:
        raise SchemaPlanError(f"the dfe-schemas apply manifest is unusable: {exc}") from exc

    topology, cluster = sense_topology(client, database, topology_setting=ch.topology)
    # 0 is the operator's way of saying "no default retention"; the renderer
    # reads None for that, and a declared ttl_days still wins either way.
    default_ttl = ch.default_ttl_days or None
    renderer = _renderer(
        manifest,
        topology=topology,
        data_database=database,
        default_ttl_days=default_ttl,
        cluster=cluster,
        broker_count=broker_count,
        kafka_tiered_storage=kafka_tiered_storage,
    )
    objects = _render_all(renderer, manifest.objects, source="core")

    refused: list[str] = []
    overlay = overlay_root()
    if overlay is not None and (overlay / "manifest.yaml").is_file():
        objects.extend(
            _overlay_objects(
                overlay,
                refused=refused,
                declared={obj.id for obj in manifest.objects},
                topology=topology,
                data_database=database,
                default_ttl_days=default_ttl,
                cluster=cluster,
                broker_count=broker_count,
                kafka_tiered_storage=kafka_tiered_storage,
            )
        )

    return SchemaPlan(
        objects=tuple(objects),
        schemas_version=schemas_version,
        topology=topology.value,
        data_database=database,
        refused=tuple(refused),
        overlay_root=str(overlay) if overlay is not None else "",
    )


def _overlay_objects(
    overlay: Path,
    *,
    refused: list[str],
    declared: set[str],
    topology: Topology,
    data_database: str,
    default_ttl_days: int | None,
    cluster: str,
    broker_count: int,
    kafka_tiered_storage: bool = False,
) -> list[RenderedObject]:
    """The overlay's own objects, minus anything that redefines a core one.

    A refusal is recorded and reported, never raised: an overlay fault must not
    stop the core apply, or one bad deployment edit takes the whole schema with
    it.
    """
    try:
        manifest = load_manifest(root=overlay)
    except ManifestError as exc:
        refused.append(f"{overlay}: the overlay manifest is unusable: {exc}")
        return []

    keep: list[ManifestObject] = []
    for obj in manifest.objects:
        if obj.id in declared:
            refused.append(f"{obj.id}: the core manifest already declares this object")
            continue
        if _redefines_core(obj):
            refused.append(f"{obj.id}: defines {obj.defines}, which is a core path")
            continue
        keep.append(obj)

    renderer = _renderer(
        manifest,
        topology=topology,
        data_database=data_database,
        default_ttl_days=default_ttl_days,
        cluster=cluster,
        broker_count=broker_count,
        kafka_tiered_storage=kafka_tiered_storage,
    )
    try:
        rendered = _render_all(renderer, tuple(keep), source="overlay")
    except SchemaPlanError as exc:
        refused.append(str(exc))
        return []
    logger.info("schema overlay read", root=str(overlay), objects=len(rendered))
    return rendered


def core_schemas_root() -> Path:
    """The schema tree the core plan is rendered from.

    ``DFE_SCHEMAS_DIR`` first, then the installed package, then the image's seed
    -- the same resolution the per-source path uses. One tree for both halves:
    two resolvers is how an applier and a generator came to read different trees
    on the same deployment.
    """
    from dfe_engine.schema.schema_loader import _resolve_schemas_root

    return _resolve_schemas_root() or schemas_root()


def render_one(
    object_id: str,
    *,
    data_database: str = "dfe",
    default_ttl_days: int | None = None,
    topology: Topology = Topology.SINGLE,
) -> RenderedObject:
    """One manifest object, rendered on its own.

    For a caller that needs a single statement rather than a plan: rendering all
    54 objects to read one of them costs every definition file in the tree.
    """
    manifest = _manifest(core_schemas_root())
    renderer = _renderer(
        manifest,
        topology=topology,
        data_database=data_database,
        default_ttl_days=default_ttl_days,
        cluster="",
        # Renders a statement, never a topic, so the replication clamp is unused.
        broker_count=1,
    )
    for obj in manifest.objects:
        if obj.id == object_id:
            return renderer.render(obj)
    raise SchemaPlanError(f"the manifest declares no object {object_id!r}")


def single_node_statement(object_id: str, *, data_database: str = "dfe") -> str:
    """One object's first statement, rendered for a single node.

    For a caller with no live client that wants to SHOW the DDL rather than apply
    it. The topology token is the only part that differs from what a cluster gets.
    """
    return render_one(object_id, data_database=data_database).statements[0]


def object_names(*object_ids: str, data_database: str = "dfe") -> dict[str, str]:
    """Manifest id to the object's real name, for a caller that only needs the name.

    A name is topology-independent, so this never reaches the server. It exists so
    a module that READS one of these tables names the manifest id rather than
    restating the table name, which is how the engine and dfe-schemas came to
    disagree about the landing table.
    """
    names = _all_object_names(core_schemas_root(), data_database)
    missing = [object_id for object_id in object_ids if object_id not in names]
    if missing:
        raise SchemaPlanError(f"the manifest declares no object(s): {', '.join(sorted(missing))}")
    return {object_id: names[object_id] for object_id in object_ids}


@lru_cache(maxsize=8)
def _all_object_names(root: Path, data_database: str) -> dict[str, str]:
    """Every manifest id to its object's name, rendered once per tree.

    Cached: the read paths ask on every call, and the answer cannot change
    without the tree changing.
    """
    manifest = _manifest(root)
    renderer = _renderer(
        manifest,
        topology=Topology.SINGLE,
        data_database=data_database,
        default_ttl_days=None,
        cluster="",
        # Renders names only, which no clamp touches; a topic path needs the real count.
        broker_count=1,
    )
    return {obj.id: renderer.render(obj).name for obj in manifest.objects}
