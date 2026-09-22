#  Project:      dfe-engine
#  File:         src/dfe_engine/exchange/sources.py
#  Purpose:      Build and apply source bundles
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Source bundle export and import.

A bundle is one source version -- its routing, its schema and its transform --
plus a document for every meta schema the schema pins name. Import writes
through ``SourceRegistry``, whose gitcrud backend makes each write one commit in
the deploy repo's ``config/sources/``.

Every check runs before the first write, because a bundle applied halfway leaves
schema pins with no definitions behind them.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

from scalo.logger import logger

from dfe_engine.exchange.models import (
    BundleSchema,
    MetaSchemaExport,
    ResourceReference,
    SourceBundle,
    SourceImportResult,
)
from dfe_engine.exchange.schemas import (
    ExchangeConflictError,
    ExchangeError,
    ExchangeUnresolvedError,
    apply_meta_schema_export,
    build_meta_schema_export,
    require_known_format,
)
from dfe_engine.source.models import Source, SourceSchema, SourceVersion

if TYPE_CHECKING:
    from dfe_engine.schema.registry import SchemaRegistry
    from dfe_engine.source.registry import SourceRegistry


def _pinned_definitions(
    schema_registry: SchemaRegistry,
    pins: SourceSchema,
) -> list[MetaSchemaExport]:
    """One document per meta schema the pins name, in pin order.

    A custom definition travels with its whole history, so the imported schema is
    the one the operator authored and ``meta_schema_version`` still selects the
    same version. A core one travels as a reference, and there
    ``meta_schema_version`` is the pin the importer must resolve.
    """
    from dfe_engine.schema.registry import SchemaNotFoundError

    wanted: list[tuple[str, str | None]] = [
        (pins.meta_schema, pins.meta_schema_version),
        (pins.derived_schema, None),
        (pins.additional_fields, None),
    ]
    documents: list[MetaSchemaExport] = []
    seen: set[str] = set()
    for reference, version in wanted:
        if not reference:
            continue
        path = reference.removesuffix(".yaml")
        if path in seen:
            continue
        seen.add(path)
        try:
            document = build_meta_schema_export(schema_registry, path)
            if version and document.resource_type == "core":
                document = build_meta_schema_export(schema_registry, path, version=version)
        except SchemaNotFoundError as exc:
            raise ExchangeUnresolvedError(
                f"Schema pin {reference!r} names {path!r}, which this deployment does not "
                "carry, so the bundle would travel with a pin and no definition"
            ) from exc
        documents.append(document)
    return documents


def build_source_bundle(
    source_registry: SourceRegistry,
    schema_registry: SchemaRegistry,
    name: str,
    *,
    version: str | None = None,
) -> SourceBundle:
    """Export one source version as a bundle.

    Args:
        source_registry: Source registry to read from.
        schema_registry: Schema registry the pins are resolved against.
        name: The ``_source`` label.
        version: Version id to export; ``None`` takes the source's ``current``.

    Returns:
        The bundle document.

    Raises:
        SourceNotFoundError: No such source.
        ExchangeError: The requested version is not defined.
        ExchangeUnresolvedError: A schema pin names nothing here.
    """
    source = source_registry.get_source(name)
    version_id = version or source.current
    if version_id not in source.versions:
        raise ExchangeError(f"Version {version_id!r} is not defined for source {name!r}")

    if source.resource_type == "core":
        return SourceBundle(
            source=source.source,
            resource_type="core",
            reference=ResourceReference(path=source.source, version=version_id),
            display_name=source.display_name,
            description=source.description,
            state=source.state,
        )

    snapshot = source.versions[version_id]
    pins = snapshot.schema_config
    schema_section = None
    if pins is not None or snapshot.header is not None:
        schema_section = BundleSchema(
            pins=pins or SourceSchema(),
            header=snapshot.header,
            definitions=_pinned_definitions(schema_registry, pins or SourceSchema()),
        )

    return SourceBundle(
        source=source.source,
        resource_type="custom",
        version=version_id,
        date_time=snapshot.date_time,
        display_name=source.display_name,
        description=source.description,
        state=source.state,
        routing=snapshot.match,
        fetcher=snapshot.fetcher,
        schema=schema_section,
        transform=snapshot.transform,
        transport=snapshot.transport,
        archive=snapshot.archive,
        views=list(snapshot.views),
    )


def _source_from_bundle(bundle: SourceBundle) -> Source:
    """The Source a bundle describes, at the version id it was exported from."""
    section = bundle.schema_section
    snapshot = SourceVersion(
        date_time=bundle.date_time or date.today().isoformat(),
        header=section.header if section else None,
        schema_config=section.pins if section else None,
        views=list(bundle.views),
        fetcher=bundle.fetcher,
        match=bundle.routing,
        transform=bundle.transform,
        transport=bundle.transport,
        archive=bundle.archive,
    )
    version_id = bundle.version or ""
    # resource_type is set here, never taken from the bundle: an import writes an
    # operator's source, and only the engine's own reconcile declares a core one.
    return Source(
        source=bundle.source,
        resource_type="custom",
        display_name=bundle.display_name,
        description=bundle.description,
        state=bundle.state,
        current=version_id,
        versions={version_id: snapshot},
    )


def apply_source_bundle(
    source_registry: SourceRegistry,
    schema_registry: SchemaRegistry,
    bundle: SourceBundle,
    *,
    created_by: str | None = None,
) -> SourceImportResult:
    """Apply a bundle to this deployment.

    A core bundle is resolved against the source the engine already reconciles
    here and never written. Everything else writes its schema definitions first,
    then the source, both through the registries that commit to git.

    Args:
        source_registry: Source registry to write through.
        schema_registry: Schema registry the definitions are written through.
        bundle: The bundle document.
        created_by: Git author for the commits.

    Returns:
        What was written or resolved.

    Raises:
        ExchangeError: The bundle cannot be applied here.
    """
    from dfe_engine.source.registry import SourceValidationError

    require_known_format(bundle.format)

    if bundle.resource_type == "core":
        return _resolve_core_source(source_registry, bundle.reference)

    name = bundle.source
    if source_registry.source_exists(name):
        raise ExchangeConflictError(f"Source {name!r} already exists in this deployment")

    section = bundle.schema_section
    definitions = list(section.definitions) if section else []
    paths = [document.path for document in definitions]
    duplicate = next((path for path in paths if paths.count(path) > 1), None)
    if duplicate is not None:
        raise ExchangeError(f"The bundle carries {duplicate!r} twice")

    try:
        source = _source_from_bundle(bundle)
    except ValueError as exc:
        raise ExchangeError(str(exc)) from exc

    # Everything the write needs is checked here, before the first one lands.
    for document in definitions:
        apply_meta_schema_export(schema_registry, document, created_by=created_by, dry_run=True)
    try:
        source_registry.validate_source(source)
    except SourceValidationError as exc:
        raise ExchangeError(str(exc)) from exc

    results = [
        apply_meta_schema_export(schema_registry, document, created_by=created_by)
        for document in definitions
    ]
    source_registry.save_source(
        source,
        created_by=created_by,
        description=f"source: import {name}",
    )
    logger.info("source bundle imported", source=name, version=source.current, schemas=len(results))
    return SourceImportResult(
        source=name,
        action="created",
        version=source.current,
        schemas=results,
    )


def _resolve_core_source(
    source_registry: SourceRegistry,
    reference: ResourceReference | None,
) -> SourceImportResult:
    """Match a core reference against the source the engine reconciles here."""
    if reference is None:
        raise ExchangeError("a core source bundle carries no reference")

    name = reference.path
    if not source_registry.source_exists(name):
        raise ExchangeUnresolvedError(
            f"Core source {name!r} is not present here. The engine reconciles it from the "
            "deployment's own settings, so an import never writes one."
        )
    if not source_registry.is_core(name):
        raise ExchangeConflictError(
            f"{name!r} is core in the exporting deployment and custom here, so the reference "
            "names two different things"
        )

    stored = source_registry.get_source(name)
    if reference.version not in stored.versions:
        available = ", ".join(sorted(stored.versions))
        raise ExchangeUnresolvedError(
            f"Core source {name!r} carries no version {reference.version!r} here "
            f"(present: {available})"
        )

    return SourceImportResult(source=name, action="resolved", version=reference.version)
