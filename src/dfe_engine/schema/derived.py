#  Project:      dfe-engine
#  File:         schema/derived.py
#  Purpose:      The derived-schema document, its validation, and its loader switches
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A derived schema is a SELECTION of columns from a meta schema.

It is not an override layer and not an append layer: the result is exactly the
``select`` list, in its order, and ``index`` is the only key an entry may add.
``type``, ``expr``, ``comment`` and ``attribute`` all resolve from the base
column -- ``expr`` above all, because dfe-loader reads that directive back out
of the ClickHouse column COMMENT and rewriting it moves where a field is read
from.

Document::

    base: meta/beats/filebeat
    base_version: "1.0.0"
    current: "1.0.0"
    versions:
      "1.0.0":
        date: "2026-09-21"
        summary: "system.auth subset"
        capture_json: false
        capture_raw: false
        select:
          - name: timestamp
          - name: user_name
            index: exact_match

``base`` is extensionless: it is a meta-schema registry path, the same string
the schemas list hands back.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from dfe_engine.source.type_registry import TypeRegistry, TypeRegistryError

DERIVED_PREFIX = "derived"
"""Top-level registry segment every derived schema is filed under."""

NO_INDEX = "none"
"""The declared value that keeps a column and drops its index."""


class DerivedSchemaError(Exception):
    """Base exception for derived-schema validation."""


class UnknownSelectionError(DerivedSchemaError):
    """A selected column name is not defined by the base meta schema."""

    def __init__(self, *, column: str, base: str, base_version: str) -> None:
        self.column = column
        self.base = base
        self.base_version = base_version
        super().__init__(
            f"column {column!r} is not defined by base schema {base!r} "
            f"version {base_version!r}. A derived schema narrows its base and "
            f"never adds to it."
        )


class UnknownIndexUseCaseError(DerivedSchemaError):
    """A declared index is not a use case the type registry knows."""

    def __init__(self, *, column: str, index: str, valid: list[str]) -> None:
        self.column = column
        self.index = index
        self.valid = valid
        super().__init__(
            f"column {column!r}: {index!r} is not an index use case. Valid: {', '.join(valid)}"
        )


class IncompatibleIndexError(DerivedSchemaError):
    """A declared index is not valid for the base column's primitive."""

    def __init__(self, *, column: str, index: str, primitive: str, valid: list[str]) -> None:
        self.column = column
        self.index = index
        self.primitive = primitive
        self.valid = valid
        super().__init__(
            f"column {column!r}: index {index!r} is not valid for primitive "
            f"{primitive!r}. Primitives that take {index!r}: {', '.join(valid)}"
        )


class MalformedIndexError(DerivedSchemaError):
    """A declared index names a known use case in a shape it does not take."""

    def __init__(self, *, column: str, index: str, detail: str) -> None:
        self.column = column
        self.index = index
        super().__init__(f"column {column!r}: {detail}")


class DerivedSelectEntry(BaseModel):
    """One selected column, optionally re-answering the question it is asked."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, description="Column name, as the base defines it")
    index: str | None = Field(
        default=None,
        description=(
            "Index use case for this column, overriding the base column's. "
            "'none' keeps the column and drops its index; omitting the key keeps "
            "the base column's use case."
        ),
    )

    @field_validator("index", mode="before")
    @classmethod
    def _empty_str_to_none(cls, value: Any) -> Any:
        return None if value == "" else value


class DerivedSchemaVersion(BaseModel):
    """One version: the ordered selection, plus the loader's catch-all switches."""

    model_config = ConfigDict(extra="forbid")

    date: str = Field(..., min_length=1, description="ISO date the version was written")
    summary: str = Field(default="", description="What this selection is for")
    capture_json: bool = Field(
        default=True,
        description=(
            "Whether dfe-loader populates _json for this table. The column and the "
            "common header stay in place either way, so the decision is reversible."
        ),
    )
    capture_raw: bool = Field(
        default=True,
        description="Whether dfe-loader populates _raw for this table",
    )
    select: list[DerivedSelectEntry] = Field(
        ...,
        min_length=1,
        description="The complete, ordered column list of the result, after the common header",
    )

    def to_yaml_dict(self) -> dict[str, Any]:
        """Serialize for YAML persistence (defaults stay off disk)."""
        data: dict[str, Any] = {"date": self.date, "summary": self.summary}
        if not self.capture_json:
            data["capture_json"] = False
        if not self.capture_raw:
            data["capture_raw"] = False
        data["select"] = [entry.model_dump(exclude_none=True) for entry in self.select]
        return data


class DerivedSchema(BaseModel):
    """A derived schema document: which base, which version of it, and the selections."""

    model_config = ConfigDict(extra="ignore")

    base: str = Field(
        ...,
        min_length=1,
        description="Meta-schema registry path, extensionless (e.g. meta/beats/filebeat)",
    )
    base_version: str = Field(
        ..., min_length=1, description="Version of the base meta schema the selection is against"
    )
    current: str = Field(..., min_length=1, description="Current version of this derived schema")
    versions: dict[str, DerivedSchemaVersion] = Field(
        ..., min_length=1, description="Version id -> selection"
    )
    path: str | None = Field(
        default=None,
        description="Registry path (derived/<group>/<name>); not stored in the YAML file",
    )
    origin: Literal["deploy", "shipped"] = Field(
        default="deploy",
        description=(
            "Which root this document was read from, when the deploy repo and the "
            "shipped schemas tree both carry derived schemas; not stored in the YAML file."
        ),
    )

    @field_validator("base")
    @classmethod
    def _base_is_extensionless(cls, value: str) -> str:
        # dfe-ui and the schemas list both speak registry paths, so a suffix here
        # would be a second spelling of one reference.
        normalised = value.replace("\\", "/").strip("/")
        if normalised.lower().endswith((".yaml", ".yml")):
            raise ValueError(
                f"base {value!r} must be a registry path without a file extension, "
                f"e.g. {normalised.rsplit('.', 1)[0]!r}"
            )
        if not normalised or any(part in ("", ".", "..") for part in normalised.split("/")):
            raise ValueError(f"base {value!r} is not a registry path")
        return normalised

    @model_validator(mode="after")
    def _current_names_a_version(self) -> DerivedSchema:
        if self.current not in self.versions:
            raise ValueError(f"current version {self.current!r} is not defined in versions")
        return self

    def version(self, version_id: str | None = None) -> DerivedSchemaVersion:
        """The named version, or ``current``."""
        wanted = version_id or self.current
        block = self.versions.get(wanted)
        if block is None:
            raise DerivedSchemaError(f"version {wanted!r} is not defined on {self.path or '?'}")
        return block

    def to_yaml_dict(self) -> dict[str, Any]:
        """Serialize for YAML persistence (excludes the registry-only ``path``)."""
        return {
            "base": self.base,
            "base_version": self.base_version,
            "current": self.current,
            "versions": {key: ver.to_yaml_dict() for key, ver in self.versions.items()},
        }


# -- Validation against a base --------------------------------


def validate_against_base(
    schema: DerivedSchema,
    base_columns: list[Any],
    *,
    registry: TypeRegistry,
    version_id: str | None = None,
) -> None:
    """Refuse a selection the base cannot satisfy, loudly and one reason at a time.

    Three failures, in the order a caller can act on them: a name the base does
    not define, an index the registry does not know, and an index the column's
    primitive cannot take.

    Raises:
        UnknownSelectionError, UnknownIndexUseCaseError, IncompatibleIndexError.
    """
    by_name = {column.name: column for column in base_columns}

    for entry in schema.version(version_id).select:
        base_column = by_name.get(entry.name)
        if base_column is None:
            raise UnknownSelectionError(
                column=entry.name, base=schema.base, base_version=schema.base_version
            )
        if entry.index is None or entry.index == NO_INDEX:
            continue
        _check_index(entry.name, entry.index, base_column.type, registry)


def _check_index(column: str, index: str, primitive: str, registry: TypeRegistry) -> None:
    """Refuse an index the registry does not know, then one the primitive cannot take."""
    try:
        valid_primitives = registry.valid_primitives_for_use_case(index)
    except TypeRegistryError as exc:
        raise UnknownIndexUseCaseError(
            column=column, index=index, valid=[*registry.use_cases, NO_INDEX]
        ) from exc

    if primitive not in valid_primitives:
        raise IncompatibleIndexError(
            column=column, index=index, primitive=primitive, valid=valid_primitives
        )

    # What is left is the argument form: similarity_search needs its dimension
    # count, everything else takes none.
    try:
        registry.validate_use_case(primitive, index)
    except TypeRegistryError as exc:
        raise MalformedIndexError(column=column, index=index, detail=str(exc)) from exc


# -- Loader capture -------------------------------------------

CAPTURE_MODE_FULL = "full"
CAPTURE_MODE_RAW_ONLY = "raw_only"
CAPTURE_MODE_JSON_ONLY = "json_only"
CAPTURE_MODE_EXTRACTED_ONLY = "extracted_only"


def capture_mode(*, capture_json: bool, capture_raw: bool) -> str:
    """The dfe-loader capture mode a pair of switches compiles to.

    full = both columns populated, raw_only = _raw alone, json_only = _json
    alone, extracted_only = neither: one loader ``CaptureMode`` per pair.
    """
    if capture_json and capture_raw:
        return CAPTURE_MODE_FULL
    if capture_raw:
        return CAPTURE_MODE_RAW_ONLY
    if capture_json:
        return CAPTURE_MODE_JSON_ONLY
    return CAPTURE_MODE_EXTRACTED_ONLY
