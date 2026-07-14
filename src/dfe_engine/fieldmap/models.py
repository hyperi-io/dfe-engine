"""Field map model — Pydantic model for standard-to-DFE field mappings.

A FieldMap defines how fields from a detection/analytics standard
(Sigma, ECS, CIM) map to DFE ClickHouse column names.

Usage:
    from dfe_engine.fieldmap.models import FieldMap

    fm = FieldMap.model_validate(yaml_data)
    fm_dict = fm.to_yaml_dict()
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field, field_validator

_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")

KNOWN_STANDARDS = frozenset({"sigma", "ecs", "cim", "ocsf"})

DEFAULT_MAP_NAME = "_default"


class FieldMap(BaseModel):
    """A mapping from a detection/analytics standard's fields to DFE columns.

    Two-tier resolution: a default map applies to all sources for a
    standard, and source-specific maps override individual entries.

    Attributes:
        standard: Standard identifier (e.g. "sigma", "ecs", "cim").
        source: Source name this map applies to, or None for default map.
        version: Standard version (e.g. "8.11" for ECS). Informational.
        description: Human-readable description.
        inherits: Reference to base map (e.g. "_default"). Declarative
            — the resolver does not auto-fetch, caller is responsible.
        mappings: Dict of standard_field → dfe_column_name.
    """

    standard: str = Field(..., description="Standard identifier (sigma, ecs, cim)")
    source: str | None = Field(
        default=None,
        description="Source name, or None for the default map",
    )
    version: str | None = Field(
        default=None,
        description="Standard version (e.g. '8.11' for ECS)",
    )
    description: str | None = Field(default=None, description="Human description")
    inherits: str | None = Field(
        default=None,
        description="Reference to base map (declarative)",
    )
    mappings: dict[str, str] = Field(
        default_factory=dict,
        description="standard_field → dfe_column_name",
    )

    @field_validator("standard")
    @classmethod
    def _validate_standard(cls, v: str) -> str:
        v = v.lower()
        if not _NAME_PATTERN.match(v):
            raise ValueError(f"Standard '{v}' must match [a-z][a-z0-9_]*")
        return v

    @field_validator("source")
    @classmethod
    def _validate_source(cls, v: str | None) -> str | None:
        if v is not None:
            v = v.lower()
            if not _NAME_PATTERN.match(v):
                raise ValueError(f"Source '{v}' must match [a-z][a-z0-9_]*")
        return v

    @property
    def registry_key(self) -> str:
        """Compound key used in the registry: '{standard}/{source_or__default}'."""
        name = self.source if self.source else DEFAULT_MAP_NAME
        return f"{self.standard}/{name}"

    @property
    def is_default(self) -> bool:
        """Whether this is the default map for its standard."""
        return self.source is None

    def to_yaml_dict(self) -> dict[str, Any]:
        """Serialize to a dict suitable for YAML output.

        Excludes None values for clean output.
        """
        data = self.model_dump(mode="json", exclude_none=True)
        for key in list(data.keys()):
            if isinstance(data[key], (dict, list)) and not data[key]:
                del data[key]
        return data
