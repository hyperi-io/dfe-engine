"""Meta schema model - Pydantic model for standard-to-DFE meta schema.

A MetaSchema defines the columns for a ClickHouse table.

Usage:
    from dfe_engine.schema.models import MetaSchema

    ms = MetaSchema.model_validate(yaml_data)
    ms_dict = ms.to_yaml_dict()
"""

from __future__ import annotations

from collections.abc import Sized
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    AliasChoices,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from dfe_engine.api.pagination import PaginatedResponse, PaginatedResponseWithObjects, PathTree
from dfe_engine.core_resources.yaml_resource_type import ResourceType
from dfe_engine.source.type_registry import (
    LOWCARDINALITY_ATTRIBUTE,
    Cardinality,
    current_use_case,
    fold_cardinality,
)


def _reject_empty_str(value: Any, info: ValidationInfo) -> Any:
    """Reject blank strings before core validation - used on Literals."""
    if isinstance(value, str) and not (value.strip()):
        raise ValueError(f"{info.field_name!r} must be a non-empty string")
    return value


def _require_non_empty[Collection: Sized](value: Collection, info: ValidationInfo) -> Collection:
    """Reject empty collections (list, dict, etc.)."""
    if len(value) == 0:
        raise ValueError(f"{info.field_name!r} must contain at least 1 element")
    return value


def _require_non_empty_str(value: str, info: ValidationInfo) -> str:
    """Reject blank/whitespace-only strings (runs after core str coercion)."""
    if not (value.strip()):
        raise ValueError(f"{info.field_name!r} must be a non-empty string")
    return value


def _require_positive_int(value: int, info: ValidationInfo) -> int:
    """Rejects zero and non-positive integers."""
    if value <= 0:
        raise ValueError(f"{info.field_name!r} must be a value greater than '0'")
    return value


# Reusable field types - centralise the validators so each model just annotates.
type NonEmptyDict[Key, Value] = Annotated[dict[Key, Value], AfterValidator(_require_non_empty)]
type NonEmptyList[Element] = Annotated[list[Element], AfterValidator(_require_non_empty)]
NonEmptyStr = Annotated[str, AfterValidator(_require_non_empty_str)]
PositiveInt = Annotated[int, AfterValidator(_require_positive_int)]


class SchemaColumn(BaseModel):
    """A column in the schema."""

    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)

    name: NonEmptyStr = Field(..., description="Name of the column")
    type: NonEmptyStr = Field(..., description="Type of the column")
    attribute: list[str] | None = Field(default=None, description="Attributes of the column")
    cardinality: Cardinality | None = Field(
        default=None,
        description=(
            "How many distinct values the column holds: low, high or unknown. "
            "'low' is what adds the LowCardinality wrapper and what lets "
            "exact_match emit set(0) rather than a bloom filter. Absent means "
            "unknown, which is the honest answer where nothing was measured."
        ),
    )
    use_case: str | None = Field(default=None, description="Use case of the column")
    default: str | None = Field(default=None, description="DEFAULT expression")
    order: int | None = Field(default=None, description="Position in ORDER BY / PRIMARY KEY")
    expr: str | None = Field(default=None, description="Expression for the column")
    comment: str | None = Field(default=None, description="Comment for the column")
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
        description="Explicit CODEC contents — required to set a codec with ch_override",
    )
    matched_searchable: list[str] = Field(
        default_factory=list,
        serialization_alias="_matched_searchable",
        description="Column fields that matched the search query (API only)",
    )

    @field_validator("attribute", mode="before")
    @classmethod
    def _coerce_attribute(cls, value: Any) -> list[str] | None:
        if value is None or value == []:
            return None
        if isinstance(value, str):
            return [value] if value else None
        return list(value)

    @field_validator(
        "use_case",
        "expr",
        "comment",
        "default",
        "ch_override",
        "codec",
        "cardinality",
        mode="before",
    )
    @classmethod
    def _empty_str_to_none(cls, value: Any) -> Any:
        if value == "":
            return None
        return value

    @field_validator("use_case", mode="before")
    @classmethod
    def _adopt_current_use_case(cls, value: Any) -> Any:
        """Read a schema stored under the retired vocabulary, and serve it current."""
        return current_use_case(value)

    @model_validator(mode="after")
    def _refuse_a_contradicted_cardinality(self) -> SchemaColumn:
        """Declaring the retired attribute against the cardinality is refused.

        Resolving it quietly would put the disagreement back that one field
        exists to remove.
        """
        contradicted = self.cardinality not in (None, "low")
        if contradicted and LOWCARDINALITY_ATTRIBUTE in (self.attribute or []):
            raise ValueError(
                f"column {self.name!r}: cardinality {self.cardinality!r} contradicts the "
                f"{LOWCARDINALITY_ATTRIBUTE} attribute; drop the attribute"
            )
        return self

    @property
    def declared_cardinality(self) -> str:
        """How many distinct values the column holds: low, high or unknown.

        The retired ``lowcardinality`` attribute still reads as ``low``, so the
        storage wrapper and the exact_match index read one answer rather than
        two nothing keeps in agreement.
        """
        return fold_cardinality(self.cardinality, self.attribute)

    def to_yaml_dict(self) -> dict[str, Any]:
        """Serialize for YAML persistence."""
        return self.model_dump(mode="python", exclude_none=True, exclude={"matched_searchable"})


class SchemaColumnWrite(SchemaColumn):
    """Column payload for API writes (create schema / add version)."""

    field_type: NonEmptyStr = Field(
        ...,
        validation_alias=AliasChoices("_field_type", "field_type"),
        serialization_alias="_field_type",
        description="Column classification (e.g. base); stored as _field_type in YAML",
    )


class SchemaVersion(BaseModel):
    """A version in the schema."""

    date: NonEmptyStr = Field(..., description="Date of the version")
    type: NonEmptyStr = Field(..., description="Type of the version")
    summary: NonEmptyStr = Field(..., description="Summary of the version")
    columns: NonEmptyList[SchemaColumn] = Field(..., description="List of columns in the version")

    def to_yaml_dict(self) -> dict[str, Any]:
        """Serialize for YAML persistence."""
        return {
            "date": self.date,
            "type": self.type,
            "summary": self.summary,
            "columns": [column.to_yaml_dict() for column in self.columns],
        }


class SchemaVersionGet(BaseModel):
    """Schema version payload for GET definition (columns paginated)."""

    date: str = Field(..., description="Date of the version")
    type: str = Field(..., description="Type of the version")
    summary: str = Field(..., description="Summary of the version")
    columns: PaginatedResponse[SchemaColumn] = Field(
        ..., description="Paginated columns for this version"
    )


class MetaSchemaGetResponse(BaseModel):
    """Meta-schema definition for a single requested version."""

    resource_type: ResourceType = Field(
        default="custom",
        description="core for system schemas, custom for user-created schemas",
    )
    current: str = Field(..., description="Current version of the schema")
    selected: str = Field(..., description="Version id requested via query parameter")
    version: SchemaVersionGet = Field(
        ..., description="Metadata and paginated columns for ``selected``"
    )
    path: str = Field(..., description="Registry path (e.g. aws/cloudtrail)")
    versions: list[str] = Field(..., description="All version identifiers defined on this schema")


class MetaSchema(BaseModel):
    """A schema for a ClickHouse table.

    Attributes:
        current: Current version of the schema.
        versions: Dictionary of versions and their metadata.
        path: Registry path key (e.g. ``aws/cloudtrail``); omitted from YAML on disk.
    """

    model_config = ConfigDict(extra="ignore")

    resource_type: ResourceType = Field(
        default="custom",
        description="core for system schemas, custom for user-created schemas",
    )
    current: NonEmptyStr = Field(..., description="Current version of the schema")
    versions: NonEmptyDict[str, SchemaVersion] = Field(
        ..., description="Dictionary of versions and their metadata"
    )
    path: str | None = Field(
        default=None,
        description="DirectoryConfigStore table key / relative path (not stored in YAML files)",
    )

    @field_validator("resource_type", mode="before")
    @classmethod
    def _coerce_resource_type(cls, value: Any) -> ResourceType:
        if value == "core":
            return "core"
        return "custom"

    @model_validator(mode="after")
    def _current_version_must_have_columns(self) -> MetaSchema:
        if not self.current:
            return self
        version = self.versions.get(self.current)
        if version is None:
            msg = f"current version '{self.current}' is not defined in versions"
            raise ValueError(msg)
        if not version.columns:
            msg = f"current version '{self.current}' must define at least one column"
            raise ValueError(msg)
        return self

    def to_yaml_dict(self) -> dict[str, Any]:
        """Serialize for YAML persistence (excludes registry-only ``path``)."""
        data: dict[str, Any] = {
            "current": self.current,
            "resource_type": self.resource_type,
            "versions": {key: ver.to_yaml_dict() for key, ver in self.versions.items()},
        }
        return data


class MetaSchemaUpdateRequest(BaseModel):
    """Partial update for meta-schema metadata (current pointer or version summary)."""

    model_config = ConfigDict(extra="forbid")

    current: str | None = Field(
        default=None,
        description="Set the schema's current version pointer",
    )
    summary: str | None = Field(
        default=None,
        description="Update summary on the version selected via query parameter",
    )

    @model_validator(mode="after")
    def _at_least_one_change(self) -> MetaSchemaUpdateRequest:
        if self.current is None and self.summary is None:
            raise ValueError("At least one of 'current' or 'summary' is required")
        return self


class MetaSchemaAddVersionRequest(BaseModel):
    """Add a new schema version (semver bump from current)."""

    model_config = ConfigDict(extra="forbid")

    type: Annotated[
        Literal["model", "addition", "revision"],
        BeforeValidator(_reject_empty_str),
    ] = Field(
        ...,
        description="Change category (semver bump from current)",
    )
    summary: str | None = Field(
        default=None,
        description="Human-readable summary stored on the new version",
    )
    columns: NonEmptyList[SchemaColumnWrite] = Field(
        ...,
        min_length=1,
        description="Complete column snapshot for the new version (at least one column)",
    )


class SchemaVersionCreate(BaseModel):
    """Initial version entry when creating a meta-schema via the API."""

    date: NonEmptyStr = Field(..., description="Date of the version")
    type: NonEmptyStr = Field(..., description="Type of the version")
    summary: NonEmptyStr = Field(..., description="Summary of the version")
    columns: NonEmptyList[SchemaColumnWrite] = Field(
        ..., description="List of columns in the version"
    )


class MetaSchemaCreateRequest(BaseModel):
    """Create a new custom meta-schema (``resource_type`` is set by the server)."""

    model_config = ConfigDict(extra="forbid")

    current: NonEmptyStr = Field(..., description="Current version of the schema")
    versions: NonEmptyDict[str, SchemaVersionCreate] = Field(
        ..., description="Dictionary of versions and their metadata"
    )
    path: str | None = Field(
        default=None,
        description="DirectoryConfigStore table key / relative path (must match URL when set)",
    )


def meta_schema_from_create(body: MetaSchemaCreateRequest, *, path: str) -> MetaSchema:
    """Build a persisted ``MetaSchema`` from a create request (always ``resource_type: custom``)."""
    versions = {
        version_id: SchemaVersion.model_validate(version.model_dump())
        for version_id, version in body.versions.items()
    }
    return MetaSchema(
        resource_type="custom",
        current=body.current,
        versions=versions,
        path=path,
    )


class MetaSchemaVersionWriteResponse(BaseModel):
    """Meta-schema state after adding a version or updating metadata."""

    path: str = Field(..., description="Registry path (e.g. ``aws/cloudtrail``)")
    current: str = Field(..., description="Current version id after the write")
    versions: list[str] = Field(..., description="All version ids on this schema")


def meta_schema_version_write_response(
    meta: MetaSchema, *, path: str
) -> MetaSchemaVersionWriteResponse:
    return MetaSchemaVersionWriteResponse(
        path=path,
        current=meta.current,
        versions=sorted(meta.versions.keys()),
    )


# ── Response models ──────────────────────────────────────────


class SchemaSummaryObject(BaseModel):
    """Summary of a schema.

    Attributes:
        name: Name of the schema.
        current: Current version of the schema.
        versions: List of versions.
        updated_at: Last updated timestamp.
        column_count: Number of columns in the schema.
    """

    name: NonEmptyStr = Field(..., description="Name of the schema")
    resource_type: ResourceType = Field(
        default="custom",
        description="core for system schemas, custom for user-created schemas",
    )
    current: NonEmptyStr = Field(..., description="Current version of the schema")
    versions: NonEmptyList[NonEmptyStr] = Field(..., description="List of versions")
    updated_at: NonEmptyStr = Field(..., description="Last updated timestamp")
    column_count: PositiveInt = Field(..., description="Number of columns in the schema")


PathTree[SchemaSummaryObject].model_rebuild()
SchemaSummary = PathTree[SchemaSummaryObject]
"""Schema list tree grouped by registry path (``PathTree`` of ``SchemaSummaryObject``)."""


class PaginatedSchemaSummaryResponse(
    PaginatedResponseWithObjects[SchemaSummaryObject, SchemaSummary]
):
    """Schema list: path tree in ``objects`` plus paginated ``items``."""

    @classmethod
    def from_summaries(
        cls,
        summaries: list[SchemaSummaryObject],
        page: int,
        per_page: int,
    ) -> PaginatedSchemaSummaryResponse:
        return cls.with_objects(
            summaries,
            page,
            per_page,
            SchemaSummary.from_paths(summaries, path=lambda obj: obj.name),
        )
