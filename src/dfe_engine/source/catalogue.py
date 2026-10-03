#  Project:      dfe-engine
#  File:         source/catalogue.py
#  Purpose:      The sources a transform already knows, offered as source definitions
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A transform that ships hundreds of ready transforms, turned into sources.

dfe-transform-elastic carries a transform per Elastic integration and a file
saying what each one is: which vendor package, which data stream, and every way
that data can reach the platform. Nothing about that is derivable, and nothing
about it is ours - so the file is DATA the engine reads, exactly as apps.yaml is,
and creating a source from an entry compiles the same write body a person would
otherwise have typed.

The catalogue is never the engine's own: the app that ships it prints it from
its own image (``dfe-transform-elastic emit-catalogue``), and the deployment
mounts the result where ``DFE_SOURCE_CATALOGUE_FILE`` says. A deployment that
mounts none offers none.

An INTAKE is a way the entry's data reaches the platform, and the three are three
different source definitions rather than three values of one: a pushed source is
a receiver match rule, a pulled one is a fetcher stanza with no rule at all. So
the set is closed here - a fourth intake is engine code either way, and refusing
an unknown one keeps the catalogue and the compiler from disagreeing silently.

See docs/data-plane/source.md for the source definition this produces.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, get_args

from pydantic import ValidationError
from ruamel.yaml import YAMLError
from scalo.logger import logger

from dfe_engine.appmgmt import catalogue as apps
from dfe_engine.appmgmt.catalogue import AppDescriptor, CatalogueSchemaLayers
from dfe_engine.manifest import ManifestError, manifest_path, read_manifest
from dfe_engine.schema.derived import DerivedSchema
from dfe_engine.source.models import (
    SOURCE_LABEL_FIELD,
    SourceFetcher,
    SourceMatch,
    SourceSchema,
    SourceTransform,
    SourceWriteRequest,
    validate_source_name,
)
from dfe_engine.transport import SourceTransport
from dfe_engine.yaml_utils import yaml_load

if TYPE_CHECKING:
    from dfe_engine.settings import DFESettings

CATALOGUE_FILE_ENV = "DFE_SOURCE_CATALOGUE_FILE"
"""Where a deployment mounts the catalogue it wants offered."""

DEFAULT_CATALOGUE_PATH = Path("/etc/dfe/catalogue/sources.yaml")
"""The conventional mount, so a chart that follows it needs no env var at all."""

CatalogueIntake = Literal["beats", "receiver", "fetcher"]
"""How an entry's data reaches the platform, and so which source definition it is."""

INTAKES: frozenset[str] = frozenset(get_args(CatalogueIntake))

BEATS_DATASET_FIELD = "data_stream.dataset"
"""Beats and Elastic Agent stamp the integration's dataset here, on every event.

The receiver's router reads a dotted path straight out of the payload, so this is
the field it tests - and it is the one field a Beats event carries that says
which integration produced it.
"""

DEFAULT_TRANSFORM = "default"
"""The transform every catalogue entry has; the others are per-entry extras."""


class SourceCatalogueError(ManifestError):
    """Raised when the source catalogue cannot be read or an entry cannot be used."""


@dataclass(frozen=True, slots=True)
class CatalogueEntry:
    """One source the shipping app already knows how to transform."""

    name: str
    """The app's own key for it, and the stem of every transform it selects."""

    package: str
    """Vendor integration package the transform came from."""

    data_stream: str
    """Data stream within that package."""

    intakes: frozenset[str]
    """Every way this source's payload can reach the platform."""

    framing: str | None
    """Pushed intakes only: whether the pipeline wants the syslog line or the body."""

    transforms: tuple[str, ...]
    """The programs the app compiled for this source, ``default`` first."""

    beats: dict[str, str]
    """The Beats module and fileset carrying the same source, when one does."""

    @property
    def dataset(self) -> str:
        """``<package>.<data_stream>`` - what a Beats event stamps as its dataset."""
        return f"{self.package}.{self.data_stream}"

    def source_name(self) -> str:
        """The source name this entry derives, or why it has to be given one.

        The catalogue names entries the way its vendor does; a source name is a
        Kubernetes label, because a source-bound app deploys an instance named
        for it. Most entries cross that gap by swapping the separator, and the
        ones that cannot are told so rather than truncated into a collision.
        """
        return validate_source_name(self.name.replace("_", "-"))


@dataclass(frozen=True, slots=True)
class SourceCatalogue:
    """A mounted catalogue, and the app whose transforms it describes."""

    app: AppDescriptor
    path: Path
    entries: dict[str, CatalogueEntry]

    @property
    def engine(self) -> str:
        """The ``transform.engine`` a source created from this catalogue names."""
        return self.app.service.removeprefix(apps.TRANSFORM_SERVICE_PREFIX)

    def entry(self, name: str) -> CatalogueEntry:
        """One entry by name, or a refusal naming the catalogue it is not in."""
        try:
            return self.entries[name]
        except KeyError:
            raise SourceCatalogueError(
                f"no catalogue entry named {name!r} in {self.path}"
            ) from None


def _entry_from(name: str, raw: object) -> CatalogueEntry:
    if not isinstance(raw, dict):
        raise SourceCatalogueError(f"catalogue entry {name!r} must be a mapping")
    try:
        intakes = {str(i) for i in raw["intakes"]}
        entry = CatalogueEntry(
            name=name,
            package=str(raw["package"]),
            data_stream=str(raw["data_stream"]),
            intakes=frozenset(intakes),
            framing=str(raw["framing"]) if raw.get("framing") else None,
            transforms=tuple(str(t) for t in raw["transforms"]),
            beats={str(k): str(v) for k, v in (raw.get("beats") or {}).items()},
        )
    except (KeyError, TypeError) as exc:
        raise SourceCatalogueError(f"catalogue entry {name!r} is missing {exc}") from exc
    unknown = entry.intakes - INTAKES
    if unknown:
        raise SourceCatalogueError(
            f"catalogue entry {name!r} declares intake(s) {', '.join(sorted(unknown))}, "
            f"which no source definition maps to; valid: {', '.join(sorted(INTAKES))}"
        )
    return entry


def load_source_catalogue(path: Path | str | None = None) -> SourceCatalogue | None:
    """Read the mounted catalogue, or None when this deployment mounts none.

    Resolution matches the app manifest's: the given path, then
    ``DFE_SOURCE_CATALOGUE_FILE``, then the conventional mount. There is
    deliberately no bundled fallback - the catalogue belongs to the app that
    ships it, and a stale copy baked in here would be offered as current.

    Only the conventional mount may be absent. A path someone NAMED and that is
    not there is a deployment that meant to mount a catalogue and did not, which
    is worth saying rather than answering with an empty list.
    """
    source = manifest_path(path, CATALOGUE_FILE_ENV, DEFAULT_CATALOGUE_PATH)
    if source is None or not source.is_file():
        if source is not None and source != DEFAULT_CATALOGUE_PATH:
            raise SourceCatalogueError(f"source catalogue not found: {source}")
        return None
    app = apps.catalogue_app(source.name)
    # catalogue_app matches on the binding's own filename, so it always has one.
    binding = app.catalogue
    if binding is None:
        raise SourceCatalogueError(f"{app.service} ships no catalogue")
    doc = read_manifest(source, what="source catalogue")
    raw = doc.get(binding.entries_key)
    if not isinstance(raw, dict) or not raw:
        raise SourceCatalogueError(
            f"source catalogue {source} carries no {binding.entries_key!r} mapping; "
            f"{app.service} declares its entries under that key"
        )
    if binding.schema_layers is None:
        logger.warning(
            f"The app manifest names no schema layers for {app.service}'s catalogue, so a "
            "source created from it binds no table schema"
        )
    return SourceCatalogue(
        app=app,
        path=source,
        entries={str(name): _entry_from(str(name), body) for name, body in raw.items()},
    )


# Read on first use, never on import: parsing the shipped catalogue costs about a
# second and most processes never serve it. Absence is cached too, so the flag.
_HELD: SourceCatalogue | None = None
_READ = False


def source_catalogue(path: Path | str | None = None) -> SourceCatalogue | None:
    """The mounted catalogue, read once and held. An explicit path is never held."""
    global _HELD, _READ
    if path is not None:
        return load_source_catalogue(path)
    if not _READ:
        _HELD = load_source_catalogue()
        _READ = True
    return _HELD


def reload_source_catalogue() -> SourceCatalogue | None:
    """Re-read the mounted catalogue, so a remounted file takes effect."""
    global _HELD, _READ
    _HELD = load_source_catalogue()
    _READ = True
    return _HELD


def schema_layers(
    entry: CatalogueEntry, layers: CatalogueSchemaLayers | None, settings: DFESettings
) -> SourceSchema | None:
    """The schema files this entry's table composes from, or None when they do not resolve.

    The app manifest names them, because the transform decides the shape of its
    own output: one meta schema for every entry, and per stream a derived
    schema that narrows it and the vendor fields appended after. Only the
    derived schema makes the table fit - the meta schema alone, or with the
    vendor fields on top, composes a CREATE wider than ClickHouse accepts for
    the widest vendors - so the layers are bound over a derived schema that
    selects from the manifest's meta schema, or not at all. The vendor fields
    join when they resolve too.

    The derived schema resolves under the deployment's own store first and the
    schemas tree after it, the order the table build reads it in; the meta
    schema and the vendor fields resolve under the schemas tree. A reference to
    a file the build cannot read fails the first deploy instead of the create,
    so nothing unresolved is bound.
    """
    if layers is None:
        return None
    from dfe_engine.schema.derived_registry import (
        derived_reference_root,
        resolve_derived_reference,
    )
    from dfe_engine.schema.schema_loader import _resolve_schemas_root, resolve_schema_yaml_path

    configured = settings.schemas.schemas_dir
    root = Path(configured) if configured else _resolve_schemas_root()
    if root is None or not resolve_schema_yaml_path(root, layers.meta_schema).is_file():
        return None

    derived = layers.derived(entry.package, entry.data_stream)
    derived_path = resolve_derived_reference(derived, derived_reference_root(settings), root)
    if not _selects_from(derived_path, layers.meta_schema):
        return None
    additional = layers.additional(entry.package, entry.data_stream)
    return SourceSchema(
        meta_schema=layers.meta_schema,
        derived_schema=derived,
        additional_fields=(
            additional if resolve_schema_yaml_path(root, additional).is_file() else None
        ),
    )


def _selects_from(path: Path, meta_schema: str) -> bool:
    """Whether a derived schema is at *path* and selects from *meta_schema*.

    A derived schema at the same stream path over another base belongs to a
    source that binds that base, and composed onto this one it selects names
    the meta schema does not define.
    """
    if not path.is_file():
        return False
    try:
        schema = DerivedSchema.model_validate(yaml_load(path))
    except (ValidationError, YAMLError) as exc:
        logger.warning(f"Derived schema {path} does not read, so it is not bound: {exc}")
        return False
    return schema.base == meta_schema


def _origin_for(
    entry: CatalogueEntry, intake: str, name: str
) -> tuple[SourceMatch | None, SourceFetcher | None]:
    """The receiver match or the fetcher stanza this intake makes the source.

    Beats stamps the integration's dataset on every event, so a Beats intake is
    recognised by content. A pushed intake is not: a syslog appliance sends
    hostnames and facilities the catalogue cannot know, so the match is the
    ``_source`` label its sender stamps - the same rule shape the engine already
    compiles for a fetched source, and the only field that is the same for every
    listener the receiver runs. A pulled intake has no rule at all; its fetcher
    stamps the label itself.
    """
    if intake == "beats":
        return (
            SourceMatch(field=BEATS_DATASET_FIELD, operator="equals", value=entry.dataset),
            None,
        )
    if intake == "receiver":
        return SourceMatch(field=SOURCE_LABEL_FIELD, operator="equals", value=name), None

    family = apps.source_type_for_package(entry.package)
    if family is None:
        raise SourceCatalogueError(
            f"catalogue entry {entry.name!r} is pulled from the {entry.package!r} package, "
            f"which no fetcher family polls; add it to the fetcher's catalogue_packages in "
            f"the app manifest, or create this source with the beats intake"
        )
    return None, SourceFetcher(source_type=family, topic="own")


def write_request_for(
    catalogue: SourceCatalogue,
    entry: CatalogueEntry,
    *,
    intake: str,
    settings: DFESettings,
    name: str | None = None,
    transform: str = DEFAULT_TRANSFORM,
    transport: SourceTransport | None = None,
    archive: bool = False,
) -> SourceWriteRequest:
    """Compile one catalogue entry into the source definition it stands for.

    Everything the entry knows becomes a field of the ordinary write body, so the
    source that comes out is indistinguishable from a hand-written one and takes
    the same write path, the same validation and the same reconcile.
    """
    if intake not in entry.intakes:
        raise SourceCatalogueError(
            f"catalogue entry {entry.name!r} does not arrive by {intake!r}; it arrives by "
            f"{', '.join(sorted(entry.intakes))}"
        )
    if transform not in entry.transforms:
        raise SourceCatalogueError(
            f"catalogue entry {entry.name!r} has no {transform!r} transform; it has "
            f"{', '.join(entry.transforms)}"
        )
    binding = catalogue.app.catalogue
    if binding is None:
        raise SourceCatalogueError(f"{catalogue.app.service} ships no catalogue")

    source_name = validate_source_name(name) if name else entry.source_name()
    match, fetcher = _origin_for(entry, intake, source_name)
    return SourceWriteRequest(
        source=source_name,
        description=f"{entry.dataset} ({intake} intake), from the {catalogue.engine} catalogue",
        schema_config=schema_layers(entry, binding.schema_layers, settings),
        transform=SourceTransform(
            engine=catalogue.engine,
            variant=binding.variant(entry.name, transform),
        ),
        transport=transport,
        archive=archive,
        match=match,
        fetcher=fetcher,
    )
