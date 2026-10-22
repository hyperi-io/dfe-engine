#  Project:      dfe-engine
#  File:         source/catalogue.py
#  Purpose:      The sources DFE ships ready to create, offered as source definitions
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The pre-canned sources dfe-schemas ships, turned into source definitions.

dfe-schemas carries one file per source under ``catalogue/``: the ``POST /sources``
body for its default intake plus a ``catalogue`` block saying which vendor
package and data stream it is, every way its data can reach the platform, and
the transforms compiled for it. Nothing about that is derivable and nothing about
it is the engine's, so it is DATA read from the installed dfe-schemas package,
and creating a source from an entry compiles the same write body a person would
otherwise have typed.

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

from dfe_schemas import schemas_root
from pydantic import ValidationError
from ruamel.yaml import YAMLError
from scalo.logger import logger

from dfe_engine.appmgmt import catalogue as apps
from dfe_engine.appmgmt.catalogue import AppDescriptor, CatalogueBinding, CatalogueSchemaLayers
from dfe_engine.manifest import ManifestError, read_manifest
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
    """One source the catalogue already knows how to transform."""

    name: str
    """The transform's own key for it, and the stem of every variant it selects."""

    package: str
    """Vendor integration package the transform came from."""

    data_stream: str
    """Data stream within that package."""

    intakes: frozenset[str]
    """Every way this source's payload can reach the platform."""

    framing: str | None
    """Pushed intakes only: whether the pipeline wants the syslog line or the body."""

    transforms: tuple[str, ...]
    """The programs the transform compiled for this source, in the catalogue's order."""

    beats: dict[str, str]
    """The Beats module and fileset carrying the same source, when one does."""

    source: str
    """The source name the catalogue gives it, already a valid Kubernetes label."""

    @property
    def dataset(self) -> str:
        """``<package>.<data_stream>`` - what a Beats event stamps as its dataset."""
        return f"{self.package}.{self.data_stream}"

    def source_name(self) -> str:
        """The source name a source created from this entry takes by default."""
        return validate_source_name(self.source)


@dataclass(frozen=True, slots=True)
class SourceCatalogue:
    """The shipped catalogue, and the app whose transforms it selects."""

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


def catalogue_dir() -> Path:
    """Where the installed dfe-schemas keeps the catalogue."""
    return schemas_root() / "catalogue"


def _app_for(engine: str, path: Path) -> tuple[AppDescriptor, CatalogueBinding]:
    service = f"{apps.TRANSFORM_SERVICE_PREFIX}{engine}"
    app = apps.APP_CATALOGUE.get(service)
    if app is None or app.catalogue is None:
        raise SourceCatalogueError(
            f"{path} names transform engine {engine!r}, but the app manifest declares no "
            f"catalogue for {service}"
        )
    return app, app.catalogue


def _entry_from(path: Path, doc: dict, binding: CatalogueBinding) -> CatalogueEntry:
    raw = doc.get("catalogue")
    if not isinstance(raw, dict):
        raise SourceCatalogueError(f"{path} carries no catalogue block")
    try:
        variants = [str(v) for v in raw["variants"]]
        parsed = [binding.parse_variant(v) for v in variants]
        if not parsed or any(p is None for p in parsed):
            raise SourceCatalogueError(
                f"{path}: variants {variants} do not follow {binding.variant_pattern!r}"
            )
        stems = {p[0] for p in parsed if p}
        if len(stems) != 1:
            raise SourceCatalogueError(
                f"{path}: variants name more than one entry: {sorted(stems)}"
            )
        transforms = [p[1] for p in parsed if p]
        entry = CatalogueEntry(
            name=stems.pop(),
            package=str(raw["package"]),
            data_stream=str(raw["data_stream"]),
            intakes=frozenset(str(i) for i in raw["intakes"]),
            framing=str(raw["framing"]) if raw.get("framing") else None,
            transforms=tuple(transforms),
            beats={str(k): str(v) for k, v in (raw.get("beats") or {}).items()},
            source=str(doc["source"]),
        )
    except (KeyError, TypeError) as exc:
        raise SourceCatalogueError(f"{path} is missing {exc}") from exc
    unknown = entry.intakes - INTAKES
    if unknown:
        raise SourceCatalogueError(
            f"{path} declares intake(s) {', '.join(sorted(unknown))}, which no source "
            f"definition maps to; valid: {', '.join(sorted(INTAKES))}"
        )
    if DEFAULT_TRANSFORM not in entry.transforms:
        raise SourceCatalogueError(f"{path} has no {DEFAULT_TRANSFORM!r} transform")
    try:
        entry.source_name()
    except ValueError as exc:
        raise SourceCatalogueError(f"{path}: {exc}") from exc
    return entry


def load_source_catalogue(directory: Path | str | None = None) -> SourceCatalogue:
    """Read every catalogue file, from dfe-schemas unless a directory is given."""
    root = Path(directory) if directory is not None else catalogue_dir()
    files = sorted(root.glob("*.yaml")) if root.is_dir() else []
    if not files:
        raise SourceCatalogueError(f"no source catalogue under {root}")
    app: AppDescriptor | None = None
    entries: dict[str, CatalogueEntry] = {}
    for path in files:
        doc = read_manifest(path, what="source catalogue entry")
        engine = str((doc.get("transform") or {}).get("engine") or "")
        file_app, binding = _app_for(engine, path)
        if app is not None and file_app is not app:
            raise SourceCatalogueError(
                f"{path} selects {file_app.service}, but the catalogue already selects "
                f"{app.service}; one catalogue serves one transform"
            )
        app = file_app
        entry = _entry_from(path, doc, binding)
        if entry.name in entries:
            raise SourceCatalogueError(f"{path} repeats catalogue entry {entry.name!r}")
        entries[entry.name] = entry
    if app is None:
        raise SourceCatalogueError(f"no source catalogue under {root}")
    if app.catalogue is not None and app.catalogue.schema_layers is None:
        logger.warning(
            f"The app manifest names no schema layers for {app.service}'s catalogue, so a "
            "source created from it binds no table schema"
        )
    return SourceCatalogue(app=app, path=root, entries=entries)


# Read on first use, never on import: parsing the catalogue costs seconds and most
# processes never serve it.
_HELD: SourceCatalogue | None = None


def source_catalogue(directory: Path | str | None = None) -> SourceCatalogue:
    """The shipped catalogue, read once and held. An explicit directory is never held."""
    global _HELD
    if directory is not None:
        return load_source_catalogue(directory)
    if _HELD is None:
        _HELD = load_source_catalogue()
    return _HELD


SchemaLayer = Literal["meta_schema", "derived_schema"]
"""A schema layer an entry's table needs. The vendor fields are optional, so never missing."""


@dataclass(frozen=True, slots=True)
class SchemaLayersResolution:
    """The schema an entry binds on this deployment, or the first layer that does not resolve."""

    schema: SourceSchema | None
    """The layers bound, or None when one does not resolve."""

    missing_layer: SchemaLayer | None = None
    """The first layer that does not resolve, or None when ``schema`` is set."""

    missing_reference: str = ""
    """That layer's reference, empty when the app manifest names no layers at all."""

    @property
    def resolves(self) -> bool:
        """Whether every layer the table needs resolved, so the schema is bound."""
        return self.schema is not None


def resolve_schema_layers(
    entry: CatalogueEntry, layers: CatalogueSchemaLayers | None, settings: DFESettings
) -> SchemaLayersResolution:
    """The schema files this entry's table composes from, or the first one that does not resolve.

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
        return SchemaLayersResolution(schema=None, missing_layer="meta_schema")
    from dfe_engine.schema.derived_registry import (
        derived_reference_root,
        resolve_derived_reference,
    )
    from dfe_engine.schema.schema_loader import _resolve_schemas_root, resolve_schema_yaml_path

    configured = settings.schemas.schemas_dir
    root = Path(configured) if configured else _resolve_schemas_root()
    if root is None or not resolve_schema_yaml_path(root, layers.meta_schema).is_file():
        return SchemaLayersResolution(
            schema=None, missing_layer="meta_schema", missing_reference=layers.meta_schema
        )

    derived = layers.derived(entry.package, entry.data_stream)
    derived_path = resolve_derived_reference(derived, derived_reference_root(settings), root)
    if not _selects_from(derived_path, layers.meta_schema):
        return SchemaLayersResolution(
            schema=None, missing_layer="derived_schema", missing_reference=derived
        )
    additional = layers.additional(entry.package, entry.data_stream)
    return SchemaLayersResolution(
        schema=SourceSchema(
            meta_schema=layers.meta_schema,
            derived_schema=derived,
            additional_fields=(
                additional if resolve_schema_yaml_path(root, additional).is_file() else None
            ),
        )
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
        schema_config=resolve_schema_layers(entry, binding.schema_layers, settings).schema,
        transform=SourceTransform(
            engine=catalogue.engine,
            variant=binding.variant(entry.name, transform),
        ),
        transport=transport,
        archive=archive,
        match=match,
        fetcher=fetcher,
    )
