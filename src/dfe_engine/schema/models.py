"""Meta schema model — Pydantic model for standard-to-DFE meta schema.

A MetaSchema defines the columns for a ClickHouse table.

Usage:
    from dfe_engine.schema.models import MetaSchema

    ms = MetaSchema.model_validate(yaml_data)
    ms_dict = ms.to_yaml_dict()
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from dfe_engine.api.pagination import PaginatedResponse


class SchemaColumn(BaseModel):
    """A column in the schema."""

    name: str = Field(..., description="Name of the column")
    type: str = Field(..., description="Type of the column")
    attribute: list[str] | None = Field(default=None, description="Attributes of the column")
    use_case: str | None = Field(default=None, description="Use case of the column")
    expr: str | None = Field(default=None, description="Expression for the column")
    comment: str | None = Field(default=None, description="Comment for the column")

    @field_validator("attribute", mode="before")
    @classmethod
    def _coerce_attribute(cls, value: Any) -> list[str] | None:
        if value is None or value == []:
            return None
        if isinstance(value, str):
            return [value] if value else None
        return list(value)

    @field_validator("use_case", "expr", "comment", mode="before")
    @classmethod
    def _empty_str_to_none(cls, value: Any) -> Any:
        if value == "":
            return None
        return value

    @field_validator("name", mode="after")
    @classmethod
    def _name_non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("name must be a non-empty string")
        return value

    def to_yaml_dict(self) -> dict[str, Any]:
        """Serialize for YAML persistence (omits None; omits empty optional strings)."""
        raw = self.model_dump(mode="python", exclude_none=True)
        optional_empty_omit = frozenset({"use_case", "expr", "comment"})
        out: dict[str, Any] = {}
        for key, value in raw.items():
            if key in optional_empty_omit and value == "":
                continue
            if key == "attribute" and value == []:
                continue
            out[key] = value
        return out


class SchemaVersion(BaseModel):
    """A version in the schema."""

    date: str = Field(..., description="Date of the version")
    type: str = Field(..., description="Type of the version")
    summary: str = Field(..., description="Summary of the version")
    columns: list[SchemaColumn] = Field(..., description="List of columns in the version")

    def to_yaml_dict(self) -> dict[str, Any]:
        """Serialize for YAML persistence."""
        return {
            "date": self.date,
            "type": self.type,
            "summary": self.summary,
            "columns": [col.to_yaml_dict() for col in self.columns],
        }


class MetaSchema(BaseModel):
    """A schema for a ClickHouse table.

    Attributes:
        current: Current version of the schema.
        versions: Dictionary of versions and their metadata.
        path: Registry path key (e.g. ``aws/cloudtrail``); omitted from YAML on disk.
    """

    model_config = ConfigDict(extra="ignore")

    current: str = Field(..., description="Current version of the schema")
    versions: dict[str, SchemaVersion] = Field(
        ..., description="Dictionary of versions and their metadata"
    )
    path: str | None = Field(
        default=None,
        description="DirectoryConfigStore table key / relative path (not stored in YAML files)",
    )

    def to_yaml_dict(self) -> dict[str, Any]:
        """Serialize for YAML persistence (excludes registry-only ``path``)."""
        data: dict[str, Any] = {
            "current": self.current,
            "versions": {key: ver.to_yaml_dict() for key, ver in self.versions.items()},
        }
        return data


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

    name: str = Field(description="Name of the schema")
    current: str = Field(description="Current version of the schema")
    versions: list[str] = Field(description="List of versions")
    updated_at: str = Field(description="Last updated timestamp")
    column_count: int = Field(description="Number of columns in the schema")


class SchemaSummary(BaseModel):
    """Tree node grouping schemas by path prefix (directory layout).

    Wire input may use path segments as sibling keys alongside ``schemas``; those
    map into ``children`` during validation. Serialized JSON uses explicit
    ``schemas`` and ``children`` at every node.

    Example (wire input shape)::

        {
            "children": {
                "azure": {
                    "activity_log": {"schemas": [SchemaSummaryObject, ...]},
                    "schemas": [],
                }
            },
            "schemas": [...],
        }
    """

    schemas: list[SchemaSummaryObject] = Field(
        default_factory=list,
        description="Schema entries defined at this path level",
    )
    children: dict[str, SchemaSummary] = Field(
        default_factory=dict,
        description="Further nesting keyed by path segment",
    )

    @model_validator(mode="before")
    @classmethod
    def _coerce_path_segments(cls, data: Any) -> Any:
        if data is None:
            return {}
        if not isinstance(data, dict):
            return data
        schemas = data.get("schemas", [])
        children = data.get("children")
        if isinstance(children, dict):
            return {"schemas": schemas, "children": children}
        return {
            "schemas": schemas,
            "children": {k: v for k, v in data.items() if k not in {"schemas", "children"}},
        }

    @classmethod
    def objects_from_list(cls, data: list[SchemaSummaryObject]) -> SchemaSummary:
        """Group flat ``name`` paths into a folder tree matching on-disk layout.

        Each segment before the last is a directory; the last segment is the
        schema id / YAML stem (e.g. ``aws/sub/path/logs`` → attach under
        ``aws → sub → path``, not under a synthetic ``logs`` child).
        """
        root: dict[str, Any] = {"schemas": [], "children": {}}

        for obj in data:
            parts = [p for p in obj.name.split("/") if p]
            if not parts:
                continue

            cur = root
            for seg in parts[:-1]:
                children = cur.setdefault("children", {})
                cur = children.setdefault(seg, {"schemas": [], "children": {}})

            cur.setdefault("schemas", []).append(obj)

        return cls.model_validate(root)


class PaginatedSchemaSummaryResponse(PaginatedResponse[SchemaSummaryObject]):
    """Schema list: full ``schema_objects`` tree plus paginated ``items``."""

    schema_objects: SchemaSummary = Field(
        description="All matching schemas as a path tree (not limited to current page)",
    )

    @classmethod
    def from_summaries(
        cls,
        summaries: list[SchemaSummaryObject],
        page: int,
        per_page: int,
    ) -> PaginatedSchemaSummaryResponse:
        paginated = PaginatedResponse.from_list(summaries, page, per_page)
        return cls(
            schema_objects=SchemaSummary.objects_from_list(summaries),
            **paginated.model_dump(),
        )
