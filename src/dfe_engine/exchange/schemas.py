#  Project:      dfe-engine
#  File:         src/dfe_engine/exchange/schemas.py
#  Purpose:      Build and apply meta-schema exchange documents
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Meta-schema export and import.

Export reads the registry; import writes back through ``SchemaRegistry``, which
commits every change to git. A ``resource_type: core`` schema is only ever
RESOLVED on import -- the definition ships with dfe-schemas, so writing a copy
would fork it and stop the deployment tracking upstream.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from scalo.logger import logger

from dfe_engine.exchange.models import (
    EXCHANGE_FORMAT,
    MetaSchemaExport,
    MetaSchemaImportResult,
    ResourceReference,
)
from dfe_engine.schema.models import MetaSchema

if TYPE_CHECKING:
    from dfe_engine.schema.registry import SchemaRegistry


class ExchangeError(Exception):
    """An exchange document cannot be built or applied here."""


class ExchangeConflictError(ExchangeError):
    """The importing deployment already holds something at the target."""


class ExchangeUnresolvedError(ExchangeError):
    """A reference names something this deployment does not carry."""


def require_known_format(document_format: int) -> None:
    """Refuse a document written by a newer engine than this one can read."""
    if document_format > EXCHANGE_FORMAT:
        raise ExchangeError(
            f"Document format {document_format} is newer than this deployment reads "
            f"(highest: {EXCHANGE_FORMAT})"
        )


def build_meta_schema_export(
    registry: SchemaRegistry,
    schema_path: str,
    *,
    version: str | None = None,
) -> MetaSchemaExport:
    """Export one meta schema, in full or -- when it is core -- as a reference.

    Args:
        registry: Schema registry to read from.
        schema_path: Registry path (e.g. ``meta/cisco_ios``).
        version: Single version to export. ``None`` exports the whole history
            for a custom schema, and pins a core one at its ``current``.

    Returns:
        The exchange document.

    Raises:
        SchemaNotFoundError: No schema at that path.
        ExchangeError: The requested version is not defined.
    """
    from dfe_engine.schema.registry import canonical_schema_path

    canonical = canonical_schema_path(schema_path)
    meta = registry.get_schema(canonical)

    pin = version or meta.current
    if pin not in meta.versions:
        raise ExchangeError(f"Version {pin!r} is not defined for meta schema {canonical!r}")

    if meta.resource_type == "core":
        return MetaSchemaExport(
            path=canonical,
            resource_type="core",
            reference=ResourceReference(path=canonical, version=pin),
        )

    versions = dict(meta.versions) if version is None else {pin: meta.versions[pin]}
    return MetaSchemaExport(
        path=canonical,
        resource_type="custom",
        current=pin,
        versions=versions,
    )


def apply_meta_schema_export(
    registry: SchemaRegistry,
    document: MetaSchemaExport,
    *,
    created_by: str | None = None,
    dry_run: bool = False,
) -> MetaSchemaImportResult:
    """Apply one meta-schema document to this deployment.

    A core document is resolved against what dfe-schemas already put here and
    never written. A custom document is saved through ``SchemaRegistry``, which
    is the path that commits to git.

    Args:
        registry: Schema registry to write through.
        document: The exchange document.
        created_by: Git author for the commit.
        dry_run: Run every check and report the outcome, writing nothing.

    Returns:
        What was done, or would be done under ``dry_run``.

    Raises:
        ExchangeError: The document cannot be applied here.
    """
    from dfe_engine.core_resources.policy import schema_registry_path_is_core
    from dfe_engine.schema.registry import SchemaValidationError, canonical_schema_path

    require_known_format(document.format)

    if document.resource_type == "core":
        return _resolve_core_meta_schema(registry, document.reference)

    canonical = canonical_schema_path(document.path)
    existing = registry.find_schema_at_location(canonical)
    if existing is not None:
        if schema_registry_path_is_core(canonical_schema_path(existing), registry):
            raise ExchangeConflictError(
                f"{canonical!r} holds a core meta schema here, which no import may overwrite"
            )
        raise ExchangeConflictError(f"A meta schema already exists at {canonical!r}")

    # resource_type is set here, never taken from the document: an import writes
    # an operator's schema, and only dfe-schemas declares a core one.
    meta = MetaSchema(
        resource_type="custom",
        current=document.current,
        versions=document.versions,
        path=canonical,
    )
    result = MetaSchemaImportResult(
        path=canonical,
        resource_type="custom",
        action="created",
        version=meta.current,
    )
    if dry_run:
        return result

    try:
        registry.save_schema(
            meta,
            created_by=created_by,
            description=f"schema: import {canonical}",
        )
    except SchemaValidationError as exc:
        raise ExchangeError(str(exc)) from exc
    logger.info("meta schema imported", path=canonical, version=meta.current)
    return result


def _resolve_core_meta_schema(
    registry: SchemaRegistry,
    reference: ResourceReference | None,
) -> MetaSchemaImportResult:
    """Match a core reference against what this deployment already carries."""
    from dfe_engine.schema.registry import SchemaNotFoundError, canonical_schema_path

    if reference is None:
        raise ExchangeError("a core meta schema document carries no reference")

    canonical = canonical_schema_path(reference.path)
    try:
        meta = registry.get_schema(canonical)
    except SchemaNotFoundError as exc:
        raise ExchangeUnresolvedError(
            f"Core meta schema {canonical!r} is not present here. It ships with dfe-schemas, "
            "so install or update that rather than importing a copy."
        ) from exc

    if meta.resource_type != "core":
        raise ExchangeConflictError(
            f"{canonical!r} is core in the exporting deployment and custom here, so the "
            "reference names two different things"
        )
    if reference.version not in meta.versions:
        available = ", ".join(sorted(meta.versions))
        raise ExchangeUnresolvedError(
            f"Core meta schema {canonical!r} carries no version {reference.version!r} here "
            f"(present: {available}). Move dfe-schemas to that version rather than importing it."
        )

    return MetaSchemaImportResult(
        path=canonical,
        resource_type="core",
        action="resolved",
        version=reference.version,
    )
