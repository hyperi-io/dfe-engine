#  Project:      dfe-engine
#  File:         src/dfe_engine/exchange/models.py
#  Purpose:      Portable documents for meta-schema and source import/export
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The documents a meta schema and a source travel in.

A ``resource_type: core`` resource travels as a REFERENCE -- its registry path
and the version it was pinned at -- never as a copy. dfe-schemas ships those
definitions and updates them centrally, so an embedded copy would make every
import a fork that stops tracking upstream. The models below refuse to hold a
reference and a body at once, so that shape cannot be written.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator

from dfe_engine.core_resources.yaml_resource_type import ResourceType
from dfe_engine.schema.models import SchemaVersion
from dfe_engine.source.models import (
    SourceFetcher,
    SourceHeader,
    SourceMatch,
    SourceSchema,
    SourceState,
    SourceTransform,
    SourceView,
)
from dfe_engine.transport import SourceTransport

EXCHANGE_FORMAT = 1
"""Document format. An importer refuses a document numbered higher than this."""


class ResourceReference(BaseModel):
    """A pre-supplied resource named and pinned, never copied."""

    path: str = Field(..., description="Registry path in the exporting deployment")
    version: str = Field(
        ...,
        description="Version the exporting deployment read it at; the importer resolves this",
    )


class MetaSchemaExport(BaseModel):
    """One meta schema, in full or as a reference.

    ``resource_type: core`` carries ``reference`` and no columns. Everything else
    carries ``current`` plus the whole ``versions`` history, because nothing
    upstream defines it.
    """

    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)

    kind: Literal["meta_schema"] = "meta_schema"
    format: int = Field(default=EXCHANGE_FORMAT, description="Document format version")
    path: str = Field(..., description="Registry path (e.g. meta/cisco_ios)")
    resource_type: ResourceType = Field(
        default="custom",
        description="core travels as a reference; custom travels in full",
    )
    current: str | None = Field(default=None, description="Current version (custom schemas only)")
    versions: dict[str, SchemaVersion] | None = Field(
        default=None,
        description="Full version history (custom schemas only)",
    )
    reference: ResourceReference | None = Field(
        default=None,
        description="Path and version pin (core schemas only)",
    )

    @field_serializer("versions")
    def _serialise_versions(
        self, versions: dict[str, SchemaVersion] | None
    ) -> dict[str, dict[str, Any]] | None:
        # The YAML persistence shape, so the document does not carry the
        # search-result field a column only has on an API read.
        if versions is None:
            return None
        return {key: version.to_yaml_dict() for key, version in versions.items()}

    @model_validator(mode="after")
    def _core_travels_as_a_reference(self) -> MetaSchemaExport:
        if self.resource_type == "core":
            if self.reference is None:
                raise ValueError(
                    "a core meta schema exports as a reference: 'reference' is required"
                )
            if self.versions or self.current:
                raise ValueError(
                    "a core meta schema must not carry its columns: an embedded copy forks the "
                    "read-only schema on every import"
                )
            return self
        if self.reference is not None:
            raise ValueError("'reference' is for a core meta schema; a custom one exports in full")
        if not self.current or not self.versions:
            raise ValueError(
                "a custom meta schema exports in full: 'current' and 'versions' are required"
            )
        if self.current not in self.versions:
            raise ValueError(f"current version {self.current!r} is not present in 'versions'")
        return self


class BundleSchema(BaseModel):
    """A source's schema: the pins it declares and the definitions those pins name."""

    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)

    pins: SourceSchema = Field(..., description="The source version's own schema block")
    header: SourceHeader | None = Field(
        default=None,
        description="Common-header profile and version, when the version declares one",
    )
    definitions: list[MetaSchemaExport] = Field(
        default_factory=list,
        description="One document per meta schema the pins name, in pin order",
    )


class SourceBundle(BaseModel):
    """One source version: its routing, its schema and its transform.

    A section is present only when that source has it -- a source with no
    transform carries no ``transform`` key rather than an empty one. A
    ``resource_type: core`` source carries ``reference`` and nothing else: the
    engine reconciles it from the deployment's own settings, so a copy would be
    refused on import anyway.
    """

    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)

    kind: Literal["source_bundle"] = "source_bundle"
    format: int = Field(default=EXCHANGE_FORMAT, description="Document format version")
    source: str = Field(..., description="The _source label")
    resource_type: ResourceType = Field(
        default="custom",
        description="core travels as a reference; custom travels in full",
    )
    reference: ResourceReference | None = Field(
        default=None,
        description="Name and version pin (core sources only)",
    )
    version: str | None = Field(
        default=None,
        description="Version id of the snapshot this bundle carries",
    )
    date_time: str | None = Field(
        default=None,
        description="Creation date of the exported snapshot; absent takes the import date",
    )
    display_name: str | None = Field(default=None, description="Human-readable display name")
    description: str | None = Field(default=None, description="Source description")
    state: SourceState = Field(default="active", description="Lifecycle state")
    routing: SourceMatch | None = Field(
        default=None,
        description="Receiver match rule (receiver-based sources)",
    )
    fetcher: SourceFetcher | None = Field(
        default=None,
        description="Fetcher origin (fetcher-based sources)",
    )
    schema_section: BundleSchema | None = Field(
        default=None,
        alias="schema",
        description="Schema pins, header and the definitions they name",
    )
    transform: SourceTransform | None = Field(
        default=None,
        description="Transform stage, when the version declares one",
    )
    transport: SourceTransport | None = Field(
        default=None,
        description="bus or direct; absent takes the importing deployment's default",
    )
    archive: bool = Field(default=False, description="Keep the record as it arrived")
    views: list[SourceView] = Field(
        default_factory=list,
        description="Naming-standard views the version exposes",
    )

    @model_validator(mode="after")
    def _core_travels_as_a_reference(self) -> SourceBundle:
        if self.resource_type == "core":
            if self.reference is None:
                raise ValueError("a core source exports as a reference: 'reference' is required")
            if self.routing or self.fetcher or self.schema_section or self.transform:
                raise ValueError(
                    "a core source must not carry its definition: the engine reconciles it from "
                    "the deployment's own settings"
                )
            return self
        if self.reference is not None:
            raise ValueError("'reference' is for a core source; a custom one exports in full")
        if not self.version:
            raise ValueError("a custom source exports a version snapshot: 'version' is required")
        if (self.routing is None) == (self.fetcher is None):
            raise ValueError(
                "a source is receiver-based (routing) or fetcher-based (fetcher): set exactly one"
            )
        return self


class MetaSchemaImportResult(BaseModel):
    """What an import did with one meta schema."""

    path: str = Field(..., description="Registry path written or resolved")
    resource_type: ResourceType = Field(..., description="core or custom")
    action: Literal["created", "resolved"] = Field(
        ...,
        description="created wrote a new custom schema; resolved matched a core one already present",
    )
    version: str = Field(..., description="Current version written, or the pin resolved")


class SourceImportResult(BaseModel):
    """What an import did with a source bundle."""

    source: str = Field(..., description="The _source label written or resolved")
    action: Literal["created", "resolved"] = Field(
        ...,
        description="created wrote the source; resolved matched a core one already present",
    )
    version: str = Field(..., description="Version id written, or the pin resolved")
    schemas: list[MetaSchemaImportResult] = Field(
        default_factory=list,
        description="One entry per meta schema the bundle carried",
    )
