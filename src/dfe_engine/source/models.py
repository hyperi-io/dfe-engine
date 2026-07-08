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
from typing import Any, Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    field_validator,
    model_validator,
)

from dfe_engine.api.pagination import PaginatedResponseWithObjects, PathTree

# _source naming: lowercase alphanumeric + underscores, starts with letter
_SOURCE_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
_SOURCE_MAX_LENGTH = 64

SourceMatchOperator = Literal[
    "equals",
    "not_equals",
    "exists",
    "includes",
    "starts_with",
    "ends_with",
]


# ---------------------------------------------------------------------------
# Schema Column (used in meta_schema, derived_schema, additional_fields)
# ---------------------------------------------------------------------------


class SchemaColumn(BaseModel):
    """A single column in a schema definition.

    See docs/SCHEMA.md for the column model:
    type + attribute + use_case + expr + comment
    """

    model_config = ConfigDict(populate_by_name=True)

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
    field_type: str | None = Field(
        default=None,
        validation_alias=AliasChoices("_field_type", "field_type"),
        serialization_alias="_field_type",
        description="Column classification (e.g. base); stored as _field_type in YAML",
    )
    ch_override: str | None = Field(
        default=None,
        description="Exact ClickHouse type — bypasses primitive mapping",
    )
    codec: str | None = Field(
        default=None,
        description="Explicit CODEC contents, emitted verbatim - required to set a codec with ch_override",
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
            errors.append(f"Column {self.name!r}: unknown primitive {self.type!r}")
            return errors  # Can't validate further

        # Validate use_case↔primitive
        if self.use_case:
            try:
                registry.validate_use_case(self.type, self.use_case)
            except Exception as e:
                errors.append(f"Column {self.name!r}: {e}")

        # Validate attribute↔primitive
        for attr in self.attribute:
            try:
                registry.validate_attribute(self.type, attr)
            except Exception as e:
                errors.append(f"Column {self.name!r}: {e}")

        # Validate ch_override
        if self.ch_override:
            try:
                registry.validate_ch_override(self.ch_override)
            except Exception as e:
                errors.append(f"Column {self.name!r}: {e}")

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

    field: str = Field(
        ...,
        description=(
            "Field to match on. Prefix with '_json.' to match a path inside the "
            "JSON column (e.g. '_json._source_fetcher'); a bare name matches a "
            "real top-level column (e.g. '_org_id')."
        ),
    )
    operator: SourceMatchOperator = Field(
        default="equals",
        description=(
            "How to compare ``field`` to ``value``: equals (default), exists, "
            "includes, starts_with, ends_with, not_equals"
        ),
    )
    value: str = Field(
        default="",
        description="Operand for the operator (not used when operator is ``exists``)",
    )

    @model_validator(mode="after")
    def _validate_value_for_operator(self) -> SourceMatch:
        if self.operator == "exists":
            return self
        if not self.value.strip():
            raise ValueError(f"match.value is required when operator is {self.operator!r}")
        return self


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
            raise ValueError(f"Invalid engine {v!r}. Valid: {', '.join(sorted(valid))}")
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
            raise ValueError(f"Invalid transform engine {v!r}. Valid: {', '.join(sorted(valid))}")
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
_FORBIDDEN_WRITE_KEYS = frozenset({"versions", "current", "deployed_version", "date_time"})
_VERSIONED_KEYS = (
    "header",
    "schema",
    "schema_config",
    "mapping_standards",
    "sigma",
    "field_mappings",
    "fetcher",
    "match",
    "transform",
)


class SourceVersion(BaseModel):
    """Versioned source configuration snapshot (schema, mappings, fetcher, etc.)."""

    model_config = ConfigDict(populate_by_name=True)

    date_time: str = Field(..., description="Version creation date (YYYY-MM-DD)")
    header: SourceHeader | None = Field(
        default=None,
        description="Common schema header configuration (optional; applied at DDL compose time)",
    )
    schema_config: SourceSchema | None = Field(
        default=None,
        description="Schema configuration (None when the version did not author one)",
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
    match: SourceMatch = Field(..., description="Receiver match rule (required)")
    transform: SourceTransform | None = Field(
        default=None, description="Transform stage (optional)"
    )

    def effective_header(self) -> SourceHeader:
        """Header for runtime/DDL resolution, defaulting the profile when unauthored."""
        return self.header or SourceHeader()

    def effective_schema(self) -> SourceSchema:
        """Schema config for runtime/DDL resolution, defaulting to empty when unauthored."""
        return self.schema_config or SourceSchema()

    def to_yaml_dict(self) -> dict[str, Any]:
        """Serialize for YAML persistence under ``versions.<id>``."""
        raw = self.model_dump(mode="json", by_alias=True, exclude_none=True)
        for key in list(raw.keys()):
            if isinstance(raw[key], (dict, list)) and not raw[key]:
                del raw[key]
        return raw


def _derive_deployed_version(
    *, existing_deployed: str | None, snapshot: SourceVersion, version_id: str
) -> str | None:
    """Track current_version unless the version owns a meta_schema table to deploy."""
    if snapshot.effective_schema().meta_schema:
        return existing_deployed
    return version_id


def _next_major_semver(current: str) -> str:
    """Bump semver major (``1.2.3`` → ``2.0.0``)."""
    parts = current.strip().split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        raise ValueError(f"Version {current!r} is not semver (expected x.x.x)")
    major = int(parts[0])
    return f"{major + 1}.0.0"


def next_major_source_version(
    existing_version_ids: dict[str, SourceVersion] | dict[str, Any],
) -> str:
    """Return the next major semver after the highest existing source version id."""
    if not existing_version_ids:
        return _DEFAULT_SOURCE_VERSION

    best = _DEFAULT_SOURCE_VERSION
    best_tuple = (1, 0, 0)
    for vid in existing_version_ids:
        parts = str(vid).split(".")
        if len(parts) != 3 or not all(p.isdigit() for p in parts):
            raise ValueError(f"Existing source version id {vid!r} is not semver (expected x.x.x)")
        tup = (int(parts[0]), int(parts[1]), int(parts[2]))
        if tup > best_tuple:
            best_tuple = tup
            best = str(vid)

    return _next_major_semver(best)


class SourceWriteRequest(BaseModel):
    """Flat source definition for create/update API (no version tree)."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    source: str | None = Field(
        default=None,
        description="Source name (_source label); required on create",
    )
    display_name: str | None = Field(default=None, description="Human-readable display name")
    description: str | None = Field(default=None, description="Source description")
    enabled: bool = Field(default=True, description="Whether the source is active")
    match: SourceMatch = Field(..., description="Receiver match rule (required)")
    header: SourceHeader | None = Field(
        default=None,
        description="Common schema header configuration for this revision",
    )
    schema_config: SourceSchema | None = Field(
        default=None,
        description="Schema configuration for this revision",
        alias="schema",
    )
    transform: SourceTransform | None = Field(
        default=None, description="Transform stage (optional, top-level)"
    )
    fetcher: SourceFetcher | None = Field(default=None, description="SaaS API fetcher (optional)")
    sigma: SourceSigma | None = Field(default=None, description="Sigma field mappings (optional)")
    mapping_standards: list[str] | None = Field(
        default=None,
        description="Standards to generate mapping views for",
    )
    field_mappings: list[str] | None = Field(
        default=None,
        description="Field map registry paths for this revision",
    )

    @model_validator(mode="before")
    @classmethod
    def _reject_version_tree_keys(cls, data: Any) -> Any:
        if isinstance(data, dict):
            forbidden = _FORBIDDEN_WRITE_KEYS.intersection(data.keys())
            if forbidden:
                names = ", ".join(sorted(forbidden))
                raise ValueError(f"Fields not allowed on write: {names}")
        return data

    def to_version_snapshot(self) -> SourceVersion:
        """Build a new immutable version entry from this write payload."""
        return SourceVersion(
            date_time=date.today().isoformat(),
            header=self.header,
            schema_config=self.schema_config or SourceSchema(),
            mapping_standards=self.mapping_standards or [],
            sigma=self.sigma,
            field_mappings=self.field_mappings,
            fetcher=self.fetcher,
            match=self.match,
            transform=self.transform,
        )


def source_from_write(write: SourceWriteRequest, *, source_name: str) -> Source:
    """Create a new Source with initial version ``1.0.0`` from a flat write body."""
    version_id = _DEFAULT_SOURCE_VERSION
    snapshot = write.to_version_snapshot()
    payload: dict[str, Any] = {
        "source": source_name,
        "display_name": write.display_name,
        "description": write.description,
        "enabled": write.enabled,
        "deployed_version": None,
        "current": version_id,
        "versions": {version_id: snapshot.model_dump(mode="json", by_alias=True)},
    }
    return Source.model_validate(payload)


def _build_merged_version_snapshot(existing: Source, write: SourceWriteRequest) -> SourceVersion:
    """Build the version snapshot for a write, inheriting unset optional fields."""
    snapshot = write.to_version_snapshot()
    if write.transform is None and existing.transform is not None:
        snapshot = snapshot.model_copy(update={"transform": existing.transform})
    return snapshot


def _schema_pin_for_bump(schema: SourceSchema) -> tuple[Any, ...]:
    """Schema references that change composed columns / deploy DDL."""
    return (
        schema.meta_schema,
        schema.meta_schema_version,
        schema.derived_schema,
        schema.additional_fields,
    )


def source_version_bump_required(previous: SourceVersion, updated: SourceVersion) -> bool:
    """True when a deployed source needs a new major version id for this snapshot change."""
    prev_schema = previous.effective_schema()
    new_schema = updated.effective_schema()
    if _schema_pin_for_bump(prev_schema) != _schema_pin_for_bump(new_schema):
        return True
    if (previous.field_mappings or []) != (updated.field_mappings or []):
        return True
    prev_sigma = previous.sigma.model_dump(mode="json") if previous.sigma else None
    new_sigma = updated.sigma.model_dump(mode="json") if updated.sigma else None
    if prev_sigma != new_sigma:
        return True
    prev_transform = previous.transform.model_dump(mode="json") if previous.transform else None
    new_transform = updated.transform.model_dump(mode="json") if updated.transform else None
    return prev_transform != new_transform


def draft_build_version_to_invalidate(existing: Source, updated: Source) -> str | None:
    """Return a draft ``current`` version id whose persisted build should be dropped after edit."""
    deployed = updated.deployed_version
    current = updated.current
    if deployed is None or deployed == current:
        return None
    if existing.current != current:
        return None
    previous = existing.versions[current]
    new_snap = updated.versions[current]
    if not source_version_bump_required(previous, new_snap):
        return None
    return current


def apply_source_write_update(existing: Source, write: SourceWriteRequest) -> Source:
    """Persist a write: bump major version only when ``current`` is deployed and pins/mappings change."""
    snapshot = _build_merged_version_snapshot(existing, write)
    current_id = existing.current
    previous = existing.versions[current_id]

    append_version = (
        existing.deployed_version is not None
        and existing.deployed_version == current_id
        and source_version_bump_required(previous, snapshot)
    )

    merged_versions = dict(existing.versions)
    if append_version:
        new_version_id = next_major_source_version(existing.versions)
        if new_version_id in existing.versions:
            raise ValueError(f"Refusing to overwrite existing version {new_version_id!r}")
        merged_versions[new_version_id] = snapshot
        target_current = new_version_id
    else:
        merged_versions[current_id] = snapshot
        target_current = current_id

    payload: dict[str, Any] = {
        "source": existing.source,
        "display_name": write.display_name
        if write.display_name is not None
        else existing.display_name,
        "description": write.description if write.description is not None else existing.description,
        "enabled": write.enabled,
        "deployed_version": existing.deployed_version,
        "current": target_current,
        "versions": {
            vid: ver.model_dump(mode="json", by_alias=True) for vid, ver in merged_versions.items()
        },
    }
    return Source.model_validate(payload)


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
    deployed_version: str | None = Field(
        default=None,
        description="Version deployed to ClickHouse / runtime (null until first deploy)",
    )
    current: str = Field(
        default=_DEFAULT_SOURCE_VERSION,
        description="Working version (latest definition)",
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
            top_match = data.pop("match", None)
            top_transform = data.pop("transform", None)
            if top_match is not None or top_transform is not None:
                for ver in data["versions"].values():
                    if not isinstance(ver, dict):
                        continue
                    if top_match is not None and "match" not in ver:
                        ver["match"] = top_match
                    if top_transform is not None and "transform" not in ver:
                        ver["transform"] = top_transform
            current = data.get("current") or data.get("deployed_version")
            if current and "current" not in data:
                data["current"] = current
            return data

        version_id = _DEFAULT_SOURCE_VERSION
        header_raw = data.pop("header", None)

        version_body: dict[str, Any] = {
            "date_time": data.pop("date_time", None) or date.today().isoformat()
        }
        if header_raw is not None:
            version_body["header"] = header_raw

        if "schema" in data:
            version_body["schema"] = data.pop("schema")
        elif "schema_config" in data:
            version_body["schema"] = data.pop("schema_config")

        for key in _VERSIONED_KEYS:
            if key in ("header", "schema", "schema_config"):
                continue
            if key in data:
                version_body[key] = data.pop(key)

        data.setdefault("current", version_id)
        data["versions"] = {version_id: version_body}
        return data

    @field_validator("source")
    @classmethod
    def _validate_source_name(cls, v: str) -> str:
        """Enforce _source naming rules."""
        if len(v) > _SOURCE_MAX_LENGTH:
            raise ValueError(f"Source name {v!r} exceeds max length of {_SOURCE_MAX_LENGTH}")
        if not _SOURCE_PATTERN.match(v):
            raise ValueError(
                f"Source name {v!r} must match [a-z][a-z0-9_]* "
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
        if self.deployed_version is not None and self.deployed_version not in self.versions:
            raise ValueError(
                f"deployed_version '{self.deployed_version}' is not defined in versions"
            )
        return self

    def runtime_version_id(self) -> str:
        """Version used for runtime accessors when ``deployed_version`` is unset."""
        return self.deployed_version or self.current

    def version(self, version_id: str | None = None) -> SourceVersion:
        """Return a specific version snapshot (defaults to deployed, else current)."""
        vid = version_id or self.runtime_version_id()
        try:
            return self.versions[vid]
        except KeyError as e:
            raise ValueError(f"Source version '{vid}' is not defined") from e

    @property
    def header(self) -> SourceHeader:
        """Deployed version header (legacy accessor; defaults the profile when unauthored)."""
        return self.version().effective_header()

    @property
    def schema_config(self) -> SourceSchema:
        """Deployed version schema config (legacy accessor; empty default when unauthored)."""
        return self.version().effective_schema()

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

    @computed_field  # type: ignore[prop-decorator]
    @property
    def match(self) -> SourceMatch | None:
        """Receiver match rule on the deployed version (serialized for API compat)."""
        return self.version().match

    @computed_field  # type: ignore[prop-decorator]
    @property
    def transform(self) -> SourceTransform | None:
        """Transform config on the deployed version (serialized for API compat)."""
        return self.version().transform

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
            "current": self.current,
            "versions": {vid: ver.to_yaml_dict() for vid, ver in sorted(self.versions.items())},
        }
        if self.deployed_version is not None:
            data["deployed_version"] = self.deployed_version
        if self.description is not None:
            data["description"] = self.description
        return data


# ── Version GET response (API) ───────────────────────────────


class SourceVersionGetResponse(BaseModel):
    """Source payload for a single requested version (mirrors ``MetaSchemaGetResponse``)."""

    model_config = ConfigDict(populate_by_name=True)

    source: str = Field(..., description="Source name (_source label)")
    display_name: str | None = Field(default=None, description="Human-readable display name")
    description: str | None = Field(default=None, description="Source description")
    enabled: bool = Field(default=True, description="Whether the source is active")
    current: str = Field(..., description="Working version id")
    deployed_version: str | None = Field(
        default=None,
        description="Version deployed to ClickHouse / runtime (null until first deploy)",
    )
    selected: str = Field(..., description="Version id requested in the URL path")
    versions: list[str] = Field(..., description="All version ids defined on this source")
    previous_deployed_versions: list[str] = Field(
        default_factory=list,
        description=(
            "Version ids with a successful deploy in source-deploys history, "
            "excluding the live deployed_version"
        ),
    )
    version: SourceVersion = Field(
        ..., description="Immutable configuration snapshot for ``selected``"
    )


# ── List / summary response models (API) ─────────────────────


class SourceSummaryObject(BaseModel):
    """Summary row for paginated source list (mirrors ``SchemaSummaryObject``)."""

    name: str = Field(description="Source name (_source label)")
    display_name: str | None = Field(default=None, description="Human-readable display name")
    description: str | None = Field(default=None, description="Source description")
    enabled: bool = Field(default=True, description="Whether the source is active")
    current: str = Field(description="Working version id")
    deployed_version: str | None = Field(
        default=None,
        description="Version deployed to ClickHouse / runtime (null until first deploy)",
    )
    versions: list[str] = Field(description="All defined version ids")
    updated_at: str = Field(default="", description="Last updated timestamp (ISO 8601)")
    header_type: str | None = Field(
        default=None,
        description="Common header profile type (deployed version)",
    )
    has_transform: bool = Field(
        default=False, description="Whether a transform stage is configured"
    )
    has_fetcher: bool = Field(default=False, description="Whether a fetcher is configured")
    mapping_standards: list[str] = Field(
        default_factory=list,
        description="Mapping standards on the deployed version",
    )


PathTree[SourceSummaryObject].model_rebuild()
SourceSummaryTree = PathTree[SourceSummaryObject]
"""Source list tree grouped by path segments derived from the source name."""


class PaginatedSourceSummaryResponse(
    PaginatedResponseWithObjects[SourceSummaryObject, SourceSummaryTree]
):
    """Source list: path tree in ``objects`` plus paginated ``items``."""

    @classmethod
    def from_summaries(
        cls,
        summaries: list[SourceSummaryObject],
        page: int,
        per_page: int,
    ) -> PaginatedSourceSummaryResponse:
        return cls.with_objects(
            summaries,
            page,
            per_page,
            SourceSummaryTree.from_paths(
                summaries,
                path=lambda obj: obj.name,
            ),
        )
