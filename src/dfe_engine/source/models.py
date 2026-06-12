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
from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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
            errors.append(f"Column '{self.name}': unknown primitive '{self.type}'")
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
    config_file: str | None = Field(default=None, description="Path to engine-specific config")
    env: dict[str, str] = Field(default_factory=dict, description="Per-transform ENV overrides")
    files: list[str] = Field(default_factory=list, description="Enrichment files (CSV, MMDB)")

    @field_validator("engine")
    @classmethod
    def _validate_engine(cls, v: str) -> str:
        valid = {"vector", "wasm"}
        if v not in valid:
            raise ValueError(f"Invalid transform engine '{v}'. Valid: {', '.join(sorted(valid))}")
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
    poll_interval_secs: int = Field(default=300, description="Polling interval in seconds")


class SourceSigma(BaseModel):
    """Sigma field mapping configuration for this source."""

    taxonomy: str | None = Field(default=None, description="Built-in mapping set (e.g. 'windows')")
    custom_mappings: dict[str, str] = Field(
        default_factory=dict,
        description="Per-source field overrides (SigmaField: column_name)",
    )


_DEFAULT_SOURCE_VERSION = "1.0.0"
_VERSIONED_KEYS = (
    "header",
    "schema",
    "schema_config",
    "mapping_standards",
    "sigma",
    "field_mappings",
    "fetcher",
)


class SourceVersion(BaseModel):
    """Versioned source configuration snapshot (schema, mappings, fetcher, etc.)."""

    model_config = ConfigDict(populate_by_name=True)

    date_time: str = Field(..., description="Version creation date (YYYY-MM-DD)")
    header: SourceHeader = Field(
        default_factory=SourceHeader,
        description="Common schema header configuration",
    )
    schema_config: SourceSchema = Field(
        default_factory=SourceSchema,
        description="Schema configuration",
        alias="schema",
    )
    mapping_standards: list[str] = Field(
        default_factory=list,
        description="Standards to generate mapping views for (e.g. sigma, ecs, cim)",
    )
    sigma: SourceSigma | None = Field(default=None, description="Sigma field mappings (optional)")
    field_mappings: list[str] | None = Field(
        default=None,
        description="Field map registry paths for this version (optional)",
    )
    fetcher: SourceFetcher | None = Field(default=None, description="SaaS API fetcher (optional)")

    def to_yaml_dict(self) -> dict[str, Any]:
        """Serialize for YAML persistence under ``versions.<id>``."""
        raw = self.model_dump(mode="json", by_alias=True, exclude_none=True)
        for key in list(raw.keys()):
            if isinstance(raw[key], (dict, list)) and not raw[key]:
                del raw[key]
        return raw


# ---------------------------------------------------------------------------
# Source — Top-Level Model
# ---------------------------------------------------------------------------


class Source(BaseModel):
    """The top-level data entity in the DFE platform.

    A Source represents a distinct data stream entering the platform.
    Everything flows from the _source label.

    See docs/SOURCE.md for the full specification.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    source: str = Field(..., description="The _source label — immutable identifier")
    display_name: str | None = Field(default=None, description="Human-readable display name")
    description: str | None = Field(default=None, description="Source description")
    enabled: bool = Field(default=True, description="Whether the source is active")
    deployed_version: str = Field(
        default=_DEFAULT_SOURCE_VERSION,
        description="Version deployed to ClickHouse / runtime",
    )
    current: str = Field(
        default=_DEFAULT_SOURCE_VERSION,
        description="Working version (latest definition)",
    )
    match: SourceMatch | None = Field(
        default=None,
        description="Receiver match rule",
    )
    transform: SourceTransform | None = Field(
        default=None, description="Transform stage (optional)"
    )
    versions: dict[str, SourceVersion] = Field(
        default_factory=dict,
        description="Version id → configuration snapshot",
    )

    @model_validator(mode="before")
    @classmethod
    def _normalize_legacy_or_versioned(cls, data: Any) -> Any:
        """Accept legacy flat YAML/API bodies and normalize to the version tree."""
        if not isinstance(data, dict):
            return data

        data = dict(data)
        if data.get("versions"):
            current = data.get("current") or data.get("deployed_version")
            if current and "current" not in data:
                data["current"] = current
            if current and "deployed_version" not in data:
                data["deployed_version"] = current
            return data

        version_id = data.get("current") or _DEFAULT_SOURCE_VERSION
        header_raw = data.pop("header", None)
        if isinstance(header_raw, dict) and header_raw.get("version"):
            version_id = str(header_raw["version"])

        version_body: dict[str, Any] = {
            "date_time": data.pop("date_time", None) or date.today().isoformat()
        }
        if header_raw is not None:
            version_body["header"] = header_raw
        else:
            version_body["header"] = {"type": "time_series", "version": version_id}

        if "schema" in data:
            version_body["schema"] = data.pop("schema")
        elif "schema_config" in data:
            version_body["schema"] = data.pop("schema_config")
        else:
            version_body["schema"] = {}

        for key in _VERSIONED_KEYS:
            if key in ("header", "schema", "schema_config"):
                continue
            if key in data:
                version_body[key] = data.pop(key)

        data.setdefault("current", version_id)
        data.setdefault("deployed_version", version_id)
        data["versions"] = {version_id: version_body}
        return data

    @field_validator("source")
    @classmethod
    def _validate_source_name(cls, v: str) -> str:
        """Enforce _source naming rules."""
        if len(v) > _SOURCE_MAX_LENGTH:
            raise ValueError(f"Source name '{v}' exceeds max length of {_SOURCE_MAX_LENGTH}")
        if not _SOURCE_PATTERN.match(v):
            raise ValueError(
                f"Source name '{v}' must match [a-z][a-z0-9_]* "
                f"(lowercase alphanumeric + underscores, starts with letter)"
            )
        return v

    @model_validator(mode="after")
    def _validate_versions_and_display_name(self) -> Source:
        """Default display_name and ensure version pointers are valid."""
        if self.display_name is None:
            self.display_name = self.source.replace("_", " ").title()

        if not self.versions:
            raise ValueError("versions must contain at least one version entry")

        if self.current not in self.versions:
            raise ValueError(f"current version '{self.current}' is not defined in versions")
        if self.deployed_version not in self.versions:
            raise ValueError(
                f"deployed_version '{self.deployed_version}' is not defined in versions"
            )
        return self

    def version(self, version_id: str | None = None) -> SourceVersion:
        """Return a specific version snapshot (defaults to deployed_version)."""
        vid = version_id or self.deployed_version
        try:
            return self.versions[vid]
        except KeyError as e:
            raise ValueError(f"Source version '{vid}' is not defined") from e

    @property
    def header(self) -> SourceHeader:
        """Deployed version header (legacy accessor)."""
        return self.version().header

    @property
    def schema_config(self) -> SourceSchema:
        """Deployed version schema config (legacy accessor)."""
        return self.version().schema_config

    @property
    def mapping_standards(self) -> list[str]:
        """Deployed version mapping standards (legacy accessor)."""
        return self.version().mapping_standards

    @property
    def sigma(self) -> SourceSigma | None:
        """Deployed version sigma config (legacy accessor)."""
        return self.version().sigma

    @property
    def fetcher(self) -> SourceFetcher | None:
        """Deployed version fetcher config (legacy accessor)."""
        return self.version().fetcher

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

        Top-level identity and routing; versioned config under ``versions``.
        """
        data: dict[str, Any] = {
            "source": self.source,
            "display_name": self.display_name,
            "enabled": self.enabled,
            "deployed_version": self.deployed_version,
            "current": self.current,
            "versions": {vid: ver.to_yaml_dict() for vid, ver in sorted(self.versions.items())},
        }
        if self.description is not None:
            data["description"] = self.description
        if self.match is not None:
            data["match"] = self.match.model_dump(mode="json", exclude_none=True)
        if self.transform is not None:
            data["transform"] = self.transform.model_dump(mode="json", exclude_none=True)
        return data
