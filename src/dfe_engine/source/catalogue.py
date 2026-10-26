#  Project:      dfe-engine
#  File:         source/catalogue.py
#  Purpose:      The sources DFE ships ready to create, read from the installed dfe-schemas package
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The source catalogue, and the write body an entry compiles to on the intake chosen.

Each ``catalogue/<source>.yaml`` in dfe-schemas is the ``POST /sources`` body for the source, whose match rule the intake chosen replaces, plus a ``catalogue`` block the write model ignores. It is read from the installed wheel, never the seeded schemas volume, because the seed never deletes and an entry dropped from the catalogue would survive there.
"""

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, get_args

import dfe_schemas
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from scalo.logger import logger

from dfe_engine.appmgmt.catalogue import APP_CATALOGUE, transform_service
from dfe_engine.appmgmt.contract import ContractError, contract
from dfe_engine.manifest import ManifestError
from dfe_engine.source.models import (
    SOURCE_LABEL_FIELD,
    SourceTransform,
    SourceWriteRequest,
    validate_source_name,
)
from dfe_engine.transport import SourceTransport

CATALOGUE_DIR = "catalogue"
"""The catalogue's directory under the dfe-schemas trees."""

CatalogueIntake = Literal["beats", "receiver", "fetcher"]
"""How an entry's data reaches the platform, and so which origin its source has."""

INTAKES: frozenset[str] = frozenset(get_args(CatalogueIntake))

DEFAULT_ORIGIN_ORDER: tuple[CatalogueIntake, ...] = ("fetcher", "beats", "receiver")
"""A create with no intake takes the first of these the entry lists.

Fetcher leads because the body carries the fetcher block exactly when it is an intake. Beats beats receiver because an entry that lists it is shipped by Elastic Agent, which stamps ``data_stream.dataset`` on every event, and the ``_source`` label a receiver source matches on is one no shipper sends.
"""

BEATS_DATASET_FIELD = "data_stream.dataset"
"""Beats and Elastic Agent stamp ``<package>.<data_stream>`` here on every event."""

TRANSFORM_CAPABILITY_KIND = "transform"
"""The capability-catalogue kind a transform app lists its compiled variants under, named for its engine."""

Availability = Literal["available", "missing_variant", "unknown"]
"""Whether the deployed transform ships a variant, with ``unknown`` kept apart from both answers."""

FILE_KEYS = frozenset(
    {
        "source",
        "display_name",
        "description",
        "match",
        "fetcher",
        "schema",
        "transform",
        "catalogue",
    }
)
"""The top-level keys a catalogue file may carry, so a key the write body would drop unread is refused."""

_held: SourceCatalogue | None = None
_HOLD_LOCK = threading.Lock()


class SourceCatalogueError(ManifestError):
    """Raised when the catalogue cannot be read or an entry cannot be used as asked."""


class _CatalogueBlock(BaseModel):
    """The ``catalogue`` block beside a file's write body, typed strictly."""

    model_config = ConfigDict(extra="ignore", strict=True)

    package: str
    data_stream: str
    intakes: list[CatalogueIntake] = Field(min_length=1)
    framing: str | None = None
    beats: dict[str, str] = Field(default_factory=dict)
    variants: list[str] = Field(default_factory=list)


@dataclass(frozen=True, slots=True)
class CatalogueEntry:
    """One catalogue file: the write body for its default intake, and its catalogue block."""

    name: str
    """The source name the entry creates, which is also its file name."""

    request: SourceWriteRequest
    """The body as the file carries it, on the default intake."""

    package: str
    data_stream: str

    intakes: tuple[CatalogueIntake, ...]
    """Every way the data can arrive, in the order the file lists them."""

    framing: str | None
    """Receiver intakes only: whether the pipeline wants the syslog line or the body."""

    beats: dict[str, str]
    """The Beats module and fileset carrying the same source, where one does."""

    variants: tuple[str, ...]
    """Every compiled transform variant for the source, the body's first."""

    @property
    def dataset(self) -> str:
        """``<package>.<data_stream>``, what a Beats event stamps as its dataset."""
        return f"{self.package}.{self.data_stream}"

    @property
    def default_intake(self) -> CatalogueIntake:
        """The intake a create takes when it names none: the first of fetcher, beats and receiver the entry lists."""
        return next(intake for intake in DEFAULT_ORIGIN_ORDER if intake in self.intakes)


@dataclass(frozen=True, slots=True)
class SourceCatalogue:
    """Every entry the catalogue directory holds, by name, in name order."""

    directory: Path
    entries: dict[str, CatalogueEntry]

    def entry(self, *, name: str) -> CatalogueEntry:
        """One entry by name, or a refusal naming it."""
        try:
            return self.entries[name]
        except KeyError:
            raise SourceCatalogueError(f"no catalogue entry named {name!r}") from None


@dataclass(frozen=True, slots=True)
class VariantAvailability:
    """Whether the deployed transform ships a variant, and the reason for the answer."""

    state: Availability
    reason: str


def _entry_from(*, path: Path) -> CatalogueEntry:
    """Read one catalogue file, refusing one that breaks the rules the compiler depends on."""
    try:
        doc = _parse(libyaml=yaml.__with_libyaml__, text=path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise SourceCatalogueError(f"it is not readable YAML: {exc}") from exc
    if not (isinstance(doc, dict)):
        raise SourceCatalogueError("it is not a mapping")
    unknown = sorted(str(key) for key in doc if key not in FILE_KEYS)
    if unknown:
        raise SourceCatalogueError(
            f"it carries key(s) a catalogue file does not take: {', '.join(unknown)}"
        )
    block = doc.pop("catalogue", None)
    if not (isinstance(block, dict)):
        raise SourceCatalogueError("it carries no 'catalogue' mapping")
    try:
        catalogue = _CatalogueBlock.model_validate(block)
    except ValidationError as exc:
        raise SourceCatalogueError(
            f"its catalogue block is not valid: {_validation_text(exc=exc)}"
        ) from exc
    try:
        request = SourceWriteRequest.model_validate(doc)
    except ValidationError as exc:
        raise SourceCatalogueError(
            f"it is not a source write body: {_validation_text(exc=exc)}"
        ) from exc
    if request.source != path.stem:
        raise SourceCatalogueError(f"it creates source {request.source!r}, not its file name")
    # Fetcher is only ever the default origin, so the body carries one exactly when it is an intake.
    if ("fetcher" in catalogue.intakes) != (request.fetcher is not None):
        raise SourceCatalogueError("it must carry fetcher exactly when fetcher is an intake")
    if request.transform is not None and not (catalogue.variants):
        raise SourceCatalogueError("its catalogue block lists no variants for its transform")
    return CatalogueEntry(
        beats=catalogue.beats,
        data_stream=catalogue.data_stream,
        framing=catalogue.framing,
        intakes=tuple(catalogue.intakes),
        name=path.stem,
        package=catalogue.package,
        request=request,
        variants=tuple(catalogue.variants) if request.transform is not None else (),
    )


def _match_for(*, entry: CatalogueEntry, intake: str, source: str) -> dict[str, str]:
    """The receiver match a non-fetcher intake gives the source."""
    if intake == "beats":
        return {"field": BEATS_DATASET_FIELD, "operator": "equals", "value": entry.dataset}
    return {"field": SOURCE_LABEL_FIELD, "operator": "equals", "value": source}


def _parse(*, libyaml: bool, text: str) -> Any:
    """The YAML document *text* holds, through libyaml when PyYAML was built with it."""
    # libyaml, because the catalogue is a thousand-odd files and ruamel's pure-Python parser takes seconds.
    if libyaml:
        return yaml.load(text, Loader=yaml.CSafeLoader)
    return yaml.load(text, Loader=yaml.SafeLoader)


def _shipped_variants(*, capabilities: list[Any], engine: str) -> list[str] | None:
    """The variant names a transform's capability catalogue lists, or None when it lists no such transform."""
    for capability in capabilities:
        if not (isinstance(capability, dict)):
            continue
        if (capability.get("kind"), capability.get("name")) != (TRANSFORM_CAPABILITY_KIND, engine):
            continue
        children = (capability.get("children")) or ([])
        return [str(child.get("name")) for child in children if isinstance(child, dict)]
    return None


def _validation_text(*, exc: ValidationError) -> str:
    """Each failed field of *exc* as ``path: message``, on one line."""
    parts = []
    for error in exc.errors():
        where = ".".join(str(part) for part in error["loc"])
        parts.append(f"{where}: {error['msg']}" if where else error["msg"])
    return ", ".join(parts)


def load_source_catalogue(*, directory: Path | None = None) -> SourceCatalogue:
    """Read every catalogue file, from the installed dfe-schemas package unless *directory* names another.

    A file that does not read as a catalogue entry is left out with one warning naming it, so one bad file never takes the rest with it.
    """
    root = directory if directory is not None else dfe_schemas.schemas_root() / CATALOGUE_DIR
    if not (root.is_dir()):
        raise SourceCatalogueError(
            f"the installed dfe-schemas {dfe_schemas.__version__!r} ships no catalogue at {str(root)!r}"
        )
    entries = {}
    for path in sorted(root.glob("*.yaml")):
        try:
            entry = _entry_from(path=path)
        except SourceCatalogueError as exc:
            logger.warning("Catalogue file skipped", file=path.name, reason=str(exc))
            continue
        entries[entry.name] = entry
    return SourceCatalogue(directory=root, entries=entries)


def source_catalogue() -> SourceCatalogue:
    """The installed catalogue, read on first use and held, because the wheel does not change under a running process."""
    global _held
    # Concurrent first callers wait for one read rather than each doing it.
    with _HOLD_LOCK:
        if _held is None:
            _held = load_source_catalogue()
        return _held


def variant_availability(
    *, contract_root: Path | None = None, transform: SourceTransform | None
) -> VariantAvailability:
    """Whether the deployed transform app ships the variant *transform* names, read off its mounted contract."""
    if (transform is None) or (transform.variant is None):
        return VariantAvailability(
            reason="the source selects no compiled variant", state="available"
        )
    service = transform_service(engine=transform.engine)
    capability = f"{TRANSFORM_CAPABILITY_KIND}/{transform.engine}"
    if service not in APP_CATALOGUE:
        return VariantAvailability(
            reason=f"the app manifest declares no {service!r}, so its variants are not known",
            state="unknown",
        )
    try:
        app_contract = contract(root=contract_root, service=service)
    except ContractError:
        return VariantAvailability(
            reason=f"the {service!r} contract is mounted but not readable; "
            f"GET /api/v1/app-contracts/{service} says why",
            state="unknown",
        )
    if not (app_contract.available):
        return VariantAvailability(
            reason=f"no {service!r} contract is mounted, so its variants are not known",
            state="unknown",
        )
    shipped = _shipped_variants(capabilities=app_contract.capabilities, engine=transform.engine)
    if shipped is None:
        return VariantAvailability(
            reason=f"the {service!r} contract lists no {capability!r} capability, so its "
            "variants are not known",
            state="unknown",
        )
    if not (shipped):
        return VariantAvailability(
            reason=f"the {service!r} contract lists no variants under {capability!r}, so they "
            "are not known",
            state="unknown",
        )
    if transform.variant in shipped:
        return VariantAvailability(
            reason=f"the deployed {service!r} ships {transform.variant!r}", state="available"
        )
    return VariantAvailability(
        reason=f"the deployed {service!r} does not ship transform variant {transform.variant!r}",
        state="missing_variant",
    )


def write_request_for(
    *,
    archive: bool = False,
    entry: CatalogueEntry,
    intake: CatalogueIntake | None = None,
    name: str | None = None,
    transport: SourceTransport | None = None,
    variant: str | None = None,
) -> SourceWriteRequest:
    """Compile *entry* into the write body for *intake*.

    The caller's name, variant, transport and archive are applied. The source records
    the catalogue entry and the intake that was selected, and a later edit leaves both
    in place.
    """
    chosen_intake = (intake) or (entry.default_intake)
    if chosen_intake not in entry.intakes:
        raise SourceCatalogueError(
            f"catalogue entry {entry.name!r} does not arrive by {chosen_intake!r}; it arrives by "
            f"{', '.join(entry.intakes)}"
        )
    if variant is not None and variant not in entry.variants:
        offered = (", ".join(entry.variants)) or ("none, as it runs no transform")
        raise SourceCatalogueError(
            f"catalogue entry {entry.name!r} has no variant {variant!r}; it has {offered}"
        )
    source = validate_source_name(name=(name) or (entry.name))
    doc = entry.request.model_dump(by_alias=True, exclude_unset=True, mode="json")
    doc["source"] = source
    doc["archive"] = archive
    doc["transport"] = transport
    doc["from_catalogue_item"] = entry.name
    doc["from_catalogue_intake"] = chosen_intake
    if chosen_intake != "fetcher":
        doc.pop("fetcher", None)
        doc["match"] = _match_for(entry=entry, intake=chosen_intake, source=source)
    if variant is not None:
        doc["transform"]["variant"] = variant
    return SourceWriteRequest.model_validate(doc)
