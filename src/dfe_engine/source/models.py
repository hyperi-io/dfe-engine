"""Source model — Pydantic models for the Source top-level data entity.

A Source represents a data stream entering the DFE platform (e.g. filebeat,
syslog, crowdstrike_edr). It contains identity, schema, and optional
fetcher/transform/rules/sigma configuration.

See docs/SOURCE.md for the full specification.

Usage:
    from dfe_engine.source.models import Source, SchemaColumn

    source = Source.model_validate(yaml_data)
    source_dict = source.model_dump(mode="json")
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

# _source naming: lowercase alphanumeric + underscores, starts with letter
_SOURCE_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
_SOURCE_MAX_LENGTH = 64


# ---------------------------------------------------------------------------
# Schema Column (used in meta_schema, derived_schema, additional_fields)
# ---------------------------------------------------------------------------


class SchemaColumn(BaseModel):
    """A single column in a schema definition.

    See docs/SCHEMA.md for the column model:
    type + attribute + use_case + expr + comment
    """

    name: str = Field(..., description="Column name")
    type: str = Field(..., description="Primitive type (string, integer, etc.)")
    attribute: list[str] = Field(
        default_factory=list,
        description="Storage attributes (lowcardinality, nullable, etc.)",
    )
    use_case: str | None = Field(
        default=None,
        description="Query use case (dimension, fulltext, range, etc.)",
    )
    default: str | None = Field(
        default=None,
        description="DEFAULT expression",
    )
    order: int | None = Field(
        default=None,
        description="Position in ORDER BY / PRIMARY KEY",
    )
    expr: str | None = Field(
        default=None,
        description="DFE directive (@source, @generated, @captured, @computed, @config)",
    )
    comment: str | None = Field(
        default=None,
        description="Human-readable column description",
    )
    ch_override: str | None = Field(
        default=None,
        description="Exact ClickHouse type — bypasses primitive mapping",
    )

    @field_validator("attribute", mode="before")
    @classmethod
    def _coerce_attribute(cls, v: Any) -> list[str]:
        """Accept a single string as a one-element list."""
        if isinstance(v, str):
            return [v]
        if v is None:
            return []
        return list(v)

    def validate_against_registry(self, registry: Any) -> list[str]:
        """Validate this column against a TypeRegistry.

        Returns a list of error messages (empty if valid).
        """
        errors: list[str] = []

        # Validate primitive
        if self.type not in registry.primitives and not self.ch_override:
            errors.append(
                f"Column '{self.name}': unknown primitive '{self.type}'"
            )
            return errors  # Can't validate further

        # Validate use_case↔primitive
        if self.use_case:
            try:
                registry.validate_use_case(self.type, self.use_case)
            except Exception as e:
                errors.append(f"Column '{self.name}': {e}")

        # Validate attribute↔primitive
        for attr in self.attribute:
            try:
                registry.validate_attribute(self.type, attr)
            except Exception as e:
                errors.append(f"Column '{self.name}': {e}")

        # Validate ch_override
        if self.ch_override:
            try:
                registry.validate_ch_override(self.ch_override)
            except Exception as e:
                errors.append(f"Column '{self.name}': {e}")

        return errors


# ---------------------------------------------------------------------------
# Source Sub-Models
# ---------------------------------------------------------------------------


class SourceHeader(BaseModel):
    """Common schema header configuration."""

    type: str = Field(
        default="time_series",
        description="Profile name (time_series, minimal, passthrough)",
    )
    version: str = Field(
        default="1.0.0",
        description="Common header version (semver)",
    )


class SourceMatch(BaseModel):
    """Receiver match rule — how the receiver identifies this source."""

    field: str = Field(..., description="JSON field to inspect")
    value: str = Field(..., description="Expected value (exact match)")


class SourceSchema(BaseModel):
    """Schema configuration for the source's ClickHouse table."""

    meta_schema: str | None = Field(
        default=None,
        description="Base field definitions (YAML file reference)",
    )
    meta_schema_version: str | None = Field(
        default=None,
        description="Meta schema version (semver)",
    )
    derived_schema: str | None = Field(
        default=None,
        description="Source-specific field overrides (optional YAML reference)",
    )
    additional_fields: str | None = Field(
        default=None,
        description="Extra fields/indexes (optional YAML reference)",
    )
    ttl_days: int | None = Field(
        default=None,
        description="Data retention in days",
    )
    engine: str = Field(
        default="MergeTree",
        description="Table engine (MergeTree, ReplicatedMergeTree, SharedMergeTree)",
    )

    @field_validator("engine")
    @classmethod
    def _validate_engine(cls, v: str) -> str:
        valid = {"MergeTree", "ReplicatedMergeTree", "SharedMergeTree"}
        if v not in valid:
            raise ValueError(f"Invalid engine '{v}'. Valid: {', '.join(sorted(valid))}")
        return v


class SourceTransform(BaseModel):
    """Transform stage configuration (vector or wasm)."""

    engine: str = Field(..., description="Transform engine (vector or wasm)")
    config_file: str | None = Field(
        default=None, description="Path to engine-specific config"
    )
    env: dict[str, str] = Field(
        default_factory=dict, description="Per-transform ENV overrides"
    )
    files: list[str] = Field(
        default_factory=list, description="Enrichment files (CSV, MMDB)"
    )

    @field_validator("engine")
    @classmethod
    def _validate_engine(cls, v: str) -> str:
        valid = {"vector", "wasm"}
        if v not in valid:
            raise ValueError(
                f"Invalid transform engine '{v}'. Valid: {', '.join(sorted(valid))}"
            )
        return v


class FetcherAuth(BaseModel):
    """Fetcher authentication configuration."""

    type: str = Field(..., description="Auth type (oauth2, api_key, basic)")
    token_url: str | None = Field(default=None, description="OAuth2 token URL")
    client_id: str | None = Field(default=None, description="OAuth2 client ID")
    client_secret: str | None = Field(default=None, description="OAuth2 client secret")
    api_key: str | None = Field(default=None, description="API key")


class SourceFetcher(BaseModel):
    """Fetcher configuration for SaaS API pull sources."""

    source_type: str = Field(..., description="Fetcher type (crowdstrike, m365, etc.)")
    base_url: str | None = Field(default=None, description="API base URL")
    auth: FetcherAuth | None = Field(default=None, description="Authentication config")
    poll_interval_secs: int = Field(
        default=300, description="Polling interval in seconds"
    )


class SourceSigma(BaseModel):
    """Sigma field mapping configuration for this source."""

    taxonomy: str | None = Field(
        default=None, description="Built-in mapping set (e.g. 'windows')"
    )
    custom_mappings: dict[str, str] = Field(
        default_factory=dict,
        description="Per-source field overrides (SigmaField: column_name)",
    )


# ---------------------------------------------------------------------------
# Source — Top-Level Model
# ---------------------------------------------------------------------------


class Source(BaseModel):
    """The top-level data entity in the DFE platform.

    A Source represents a distinct data stream entering the platform.
    Everything flows from the _source label.

    See docs/SOURCE.md for the full specification.
    """

    source: str = Field(..., description="The _source label — immutable identifier")
    display_name: str | None = Field(
        default=None, description="Human-readable display name"
    )
    description: str | None = Field(default=None, description="Source description")
    enabled: bool = Field(default=True, description="Whether the source is active")

    header: SourceHeader = Field(
        default_factory=SourceHeader,
        description="Common schema header configuration",
    )
    match: SourceMatch | None = Field(
        default=None,
        description="Receiver match rule",
    )
    schema_config: SourceSchema = Field(
        default_factory=SourceSchema,
        description="Schema configuration",
        alias="schema",
    )
    transform: SourceTransform | None = Field(
        default=None, description="Transform stage (optional)"
    )
    fetcher: SourceFetcher | None = Field(
        default=None, description="SaaS API fetcher (optional)"
    )
    sigma: SourceSigma | None = Field(
        default=None, description="Sigma field mappings (optional)"
    )
    mapping_standards: list[str] = Field(
        default_factory=list,
        description="Standards to generate mapping views for (e.g. sigma, ecs, cim)",
    )

    model_config = {
        "populate_by_name": True,
    }

    @field_validator("source")
    @classmethod
    def _validate_source_name(cls, v: str) -> str:
        """Enforce _source naming rules."""
        if len(v) > _SOURCE_MAX_LENGTH:
            raise ValueError(
                f"Source name '{v}' exceeds max length of {_SOURCE_MAX_LENGTH}"
            )
        if not _SOURCE_PATTERN.match(v):
            raise ValueError(
                f"Source name '{v}' must match [a-z][a-z0-9_]* "
                f"(lowercase alphanumeric + underscores, starts with letter)"
            )
        return v

    @model_validator(mode="after")
    def _set_display_name(self) -> Source:
        """Default display_name to title-cased source name."""
        if self.display_name is None:
            self.display_name = self.source.replace("_", " ").title()
        return self

    # -----------------------------------------------------------------
    # Derived properties
    # -----------------------------------------------------------------

    @property
    def topic_land(self) -> str:
        """Kafka topic for raw data from receiver."""
        return f"{self.source}_land"

    @property
    def topic_load(self) -> str | None:
        """Kafka topic for transformed data (only if transform exists)."""
        return f"{self.source}_load" if self.transform else None

    @property
    def table_name(self) -> str:
        """ClickHouse table name (same as _source label)."""
        return self.source

    def to_yaml_dict(self) -> dict[str, Any]:
        """Serialize to a dict suitable for YAML output.

        Uses 'schema' key (not 'schema_config') for YAML compatibility.
        Excludes None values for clean output.
        """
        data = self.model_dump(mode="json", by_alias=True, exclude_none=True)
        # Remove empty containers
        for key in list(data.keys()):
            if isinstance(data[key], (dict, list)) and not data[key]:
                del data[key]
        return data
