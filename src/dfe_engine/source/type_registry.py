"""Type Registry — Canonical primitive-to-ClickHouse type mapping.

Maps simplified primitives (string, integer, text, etc.) to ClickHouse
implementation details (types, codecs, nullable defaults). Validates
use_case and attribute constraints against primitives.

Data SMEs use primitives in their schema YAML. The engine uses this
registry to generate correct ClickHouse DDL.

Usage:
    from dfe_engine.source.type_registry import TypeRegistry

    registry = TypeRegistry.default()
    resolved = registry.resolve("string", cardinality="low")
    # ResolvedType(ch_type='LowCardinality(Nullable(String))', codec='ZSTD(1)')
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from dfe_engine.yaml_utils import yaml_load

# How many distinct values a column holds. One declaration, because it decides
# both the LowCardinality wrapper and whether exact_match can hold every value.
# The vocabulary is dfe-schemas registries/types.yaml, under `cardinality`.
Cardinality = Literal["low", "high", "unknown"]
CARDINALITIES = ("low", "high", "unknown")
DEFAULT_CARDINALITY = "unknown"
LOW_CARDINALITY = "low"
# The retired spelling: a column declaring it means `cardinality: low`.
LOWCARDINALITY_ATTRIBUTE = "lowcardinality"


def fold_cardinality(cardinality: str | None, attributes: Iterable[str] | None) -> str:
    """One cardinality, with the retired ``lowcardinality`` attribute folded in.

    26 shipped schemas still carry the attribute, so it reads as ``low`` rather
    than becoming a second answer to the same question.
    """
    if cardinality:
        return cardinality
    return (
        LOW_CARDINALITY if LOWCARDINALITY_ATTRIBUTE in (attributes or []) else DEFAULT_CARDINALITY
    )


@dataclass(frozen=True)
class ResolvedType:
    """Result of resolving a primitive to a ClickHouse type."""

    ch_type: str
    codec: str | None


class TypeRegistryError(Exception):
    """Base exception for type registry errors."""


class UnknownPrimitiveError(TypeRegistryError):
    """Primitive type not found in the registry."""


class InvalidUseCaseError(TypeRegistryError):
    """Use case is not valid for the given primitive."""


class InvalidAttributeError(TypeRegistryError):
    """Attribute is not valid for the given primitive."""


class InvalidChOverrideError(TypeRegistryError):
    """ch_override value is not in the supported ClickHouse types catalogue."""


class InvalidCardinalityError(TypeRegistryError):
    """Cardinality is not one of the values the registry declares."""


# A use case is a name, optionally carrying one integer argument.
_USE_CASE_RE = re.compile(r"^(?P<name>[a-z_]+)(?:\((?P<arg>\d+)\))?$")


def split_use_case(declared: str | None) -> tuple[str | None, int | None]:
    """Split a declared use case into its name and its optional integer argument.

    ``similarity_search(768)`` is the only shape that carries one, because
    ClickHouse cannot infer a vector's dimension count from the column.
    """
    if not declared:
        return None, None
    match = _USE_CASE_RE.match(declared.strip())
    if match is None:
        raise InvalidUseCaseError(
            f"use case {declared!r} is not a name, optionally with an integer "
            f"argument -- for example 'word_search' or 'similarity_search(768)'"
        )
    arg = match.group("arg")
    return match.group("name"), int(arg) if arg else None


# The names dfe-schemas retired when a use case stopped naming the ClickHouse
# index and started naming the question. Each maps to the name that renders the
# same index, so a translated column keeps the index it was created with.
RETIRED_USE_CASES: dict[str, str] = {
    "fulltext": "word_search",
    "text_search": "substring_search",
    "bloom": "exact_match",
}


def current_use_case(declared: Any) -> Any:
    """*declared* under its current name, translating a retired one.

    A schema stored before the rename carries the old word and the registry
    rejects it, so every edit to that schema fails on a column the operator never
    touched. Refusing an unknown use case stays the registry's job: anything this
    cannot translate passes through.
    """
    if not isinstance(declared, str):
        return declared
    return RETIRED_USE_CASES.get(declared.strip(), declared)


class TypeRegistry:
    """Canonical registry mapping primitives to ClickHouse types.

    Loaded from dfe-schemas ``registries/types.yaml``. Provides:
    - resolve(): primitive → full CH type with wrapping + codec
    - validate_use_case(): enforce 1:M use_case↔primitive constraint
    - validate_attribute(): enforce 1:M attribute↔primitive constraint
    - validate_ch_override(): check against supported CH types catalogue
    """

    def __init__(self, data: dict[str, Any]) -> None:
        if not (isinstance(data, dict)) or not (isinstance(data.get("primitives"), dict)):
            raise TypeRegistryError("type registry must map its primitives under 'primitives'")
        self._primitives: dict[str, dict[str, Any]] = data["primitives"]
        self._use_cases: dict[str, dict[str, Any]] = data.get("use_cases", {})
        self._attributes: dict[str, dict[str, Any]] = data.get("attributes", {})
        self._ch_overrides: dict[str, list[str]] = data.get("ch_overrides", {})
        self._cardinality: dict[str, Any] = data.get("cardinality") or {}

        # Pre-compile parameterised override patterns
        self._override_patterns: list[re.Pattern] = [
            re.compile(f"^{pattern}$") for pattern in self._ch_overrides.get("parameterised", [])
        ]
        self._override_exact: set[str] = set(self._ch_overrides.get("exact", []))

    @classmethod
    def default(cls) -> TypeRegistry:
        """Load the type registry dfe-schemas ships as ``registries/types.yaml``."""
        from dfe_engine.schema.schema_loader import resolve_registry_path

        return cls(yaml_load(resolve_registry_path("types.yaml")))

    @classmethod
    def from_file(cls, path: str | Path) -> TypeRegistry:
        """Load a type registry from a custom YAML file."""
        data = yaml_load(path)
        return cls(data)

    # -----------------------------------------------------------------
    # Resolution
    # -----------------------------------------------------------------

    def resolve(
        self,
        primitive: str,
        *,
        attributes: list[str] | None = None,
        use_case: str | None = None,
        ch_override: str | None = None,
        cardinality: str | None = None,
    ) -> ResolvedType:
        """Resolve a primitive to a ClickHouse column type.

        Args:
            primitive: Primitive type name (e.g. 'string', 'integer').
            attributes: Storage attributes (e.g. ['not_null']).
            use_case: Query use case (e.g. 'dimension'). Validated only.
            ch_override: Exact ClickHouse type (bypasses primitive mapping).
            cardinality: 'low', 'high' or 'unknown'. Only 'low' adds the
                LowCardinality wrapper. Left out, it is read off the retired
                'lowcardinality' attribute.

        Returns:
            ResolvedType with the full CH type string and codec.

        Raises:
            UnknownPrimitiveError: Unknown primitive.
            InvalidUseCaseError: Use case not valid for primitive.
            InvalidAttributeError: Attribute not valid for primitive.
            InvalidChOverrideError: ch_override not in catalogue.
        """
        attributes = attributes or []
        declared = fold_cardinality(cardinality, attributes)

        # Validate primitive exists
        if primitive not in self._primitives:
            raise UnknownPrimitiveError(
                f"Unknown primitive '{primitive}'. Valid: {', '.join(sorted(self._primitives))}"
            )

        # Validate use_case constraint
        if use_case:
            self.validate_use_case(primitive, use_case)

        # Validate attribute constraints
        for attr in attributes:
            self.validate_attribute(primitive, attr)

        # ch_override: validate and use verbatim
        if ch_override:
            self.validate_ch_override(ch_override)
            ch_type = ch_override
            codec = None
            # Apply attributes even with ch_override
            ch_type = self._wrap(ch_type, attributes, False, declared)
            return ResolvedType(ch_type=ch_type, codec=codec)

        # Normal resolution from primitive
        prim_def = self._primitives[primitive]
        base_type = prim_def["ch_type"]
        codec = prim_def.get("codec")
        nullable_default = prim_def.get("nullable", True)

        ch_type = self._wrap(base_type, attributes, nullable_default, declared)
        return ResolvedType(ch_type=ch_type, codec=codec)

    def _wrap(
        self,
        base_type: str,
        attributes: list[str],
        nullable_default: bool,
        cardinality: str,
    ) -> str:
        """Apply the Nullable and LowCardinality wrapping to a base type.

        Only ``cardinality: low`` adds LowCardinality: on a high-cardinality
        column the dictionary costs more than it saves, and ``unknown`` is the
        safe way to be wrong.
        """
        # Determine nullability
        nullable = nullable_default
        if "nullable" in attributes:
            nullable = True
        if "not_null" in attributes:
            nullable = False

        ch_type = base_type

        # Nullable wraps inner (ClickHouse: LowCardinality(Nullable(T)))
        if nullable:
            ch_type = f"Nullable({ch_type})"

        # LowCardinality wraps outer
        if cardinality == LOW_CARDINALITY:
            ch_type = f"LowCardinality({ch_type})"

        return ch_type

    # -----------------------------------------------------------------
    # Validation
    # -----------------------------------------------------------------

    def validate_use_case(self, primitive: str, use_case: str) -> None:
        """Validate that a use case is valid for a primitive.

        Accepts the declared form, argument and all: a use case the registry
        marks as taking parameters must carry one, and one that does not must
        not.

        Raises:
            UnknownPrimitiveError: Unknown primitive.
            InvalidUseCaseError: Use case not valid for primitive.
        """
        if primitive not in self._primitives:
            raise UnknownPrimitiveError(
                f"Unknown primitive '{primitive}'. Valid: {', '.join(sorted(self._primitives))}"
            )

        name, argument = split_use_case(use_case)

        if name not in self._use_cases:
            raise InvalidUseCaseError(
                f"Unknown use case '{use_case}'. Valid: {', '.join(sorted(self._use_cases))}"
            )

        parameters = self._use_cases[name].get("parameters") or []
        if parameters and argument is None:
            raise InvalidUseCaseError(
                f"Use case '{name}' needs its {parameters[0]}, as '{name}(<{parameters[0]}>)'"
            )
        if argument is not None and not parameters:
            raise InvalidUseCaseError(f"Use case '{name}' takes no argument")

        valid = self._use_cases[name]["valid_primitives"]
        if primitive not in valid:
            raise InvalidUseCaseError(
                f"Use case '{name}' is not valid for primitive '{primitive}'. "
                f"Valid primitives for '{name}': {', '.join(valid)}"
            )

    def validate_attribute(self, primitive: str, attribute: str) -> None:
        """Validate that an attribute is valid for a primitive.

        Raises:
            UnknownPrimitiveError: Unknown primitive.
            InvalidAttributeError: Attribute not valid for primitive.
        """
        if primitive not in self._primitives:
            raise UnknownPrimitiveError(
                f"Unknown primitive '{primitive}'. Valid: {', '.join(sorted(self._primitives))}"
            )

        if attribute not in self._attributes:
            raise InvalidAttributeError(
                f"Unknown attribute '{attribute}'. Valid: {', '.join(sorted(self._attributes))}"
            )

        valid = self._attributes[attribute]["valid_primitives"]
        if valid == "all":
            return

        if primitive not in valid:
            raise InvalidAttributeError(
                f"Attribute '{attribute}' is not valid for primitive '{primitive}'. "
                f"Valid primitives for '{attribute}': {', '.join(valid)}"
            )

    def validate_cardinality(self, cardinality: str) -> None:
        """Validate a declared cardinality against the registry's vocabulary.

        Raises:
            InvalidCardinalityError: Cardinality not in the vocabulary.
        """
        if cardinality not in self.cardinalities:
            raise InvalidCardinalityError(
                f"Unknown cardinality '{cardinality}'. Valid: {', '.join(self.cardinalities)}"
            )

    def validate_ch_override(self, ch_override: str) -> None:
        """Validate that a ch_override value is a supported ClickHouse type.

        Raises:
            InvalidChOverrideError: ch_override not in catalogue.
        """
        # Check exact matches
        if ch_override in self._override_exact:
            return

        # Check parameterised patterns
        for pattern in self._override_patterns:
            if pattern.match(ch_override):
                return

        raise InvalidChOverrideError(
            f"ch_override '{ch_override}' is not a supported ClickHouse type. "
            f"See docs/data-plane/schema.md for the full override catalogue."
        )

    # -----------------------------------------------------------------
    # Introspection
    # -----------------------------------------------------------------

    @property
    def primitives(self) -> list[str]:
        """List all known primitive type names."""
        return sorted(self._primitives.keys())

    @property
    def use_cases(self) -> list[str]:
        """List all known use case names."""
        return sorted(self._use_cases.keys())

    @property
    def attribute_names(self) -> list[str]:
        """List all known attribute names."""
        return sorted(self._attributes.keys())

    @property
    def cardinalities(self) -> list[str]:
        """The cardinality vocabulary, from the registry or the package default."""
        return list(self._cardinality.get("values") or CARDINALITIES)

    def valid_primitives_for_use_case(self, use_case: str) -> list[str]:
        """Get the list of valid primitives for a use case."""
        name, _ = split_use_case(use_case)
        if name not in self._use_cases:
            raise InvalidUseCaseError(f"Unknown use case '{use_case}'")
        return list(self._use_cases[name]["valid_primitives"])

    def valid_primitives_for_attribute(self, attribute: str) -> list[str] | str:
        """Get the list of valid primitives for an attribute (or 'all')."""
        if attribute not in self._attributes:
            raise InvalidAttributeError(f"Unknown attribute '{attribute}'")
        valid = self._attributes[attribute]["valid_primitives"]
        return "all" if valid == "all" else list(valid)

    def primitive_defaults(self, primitive: str) -> dict[str, Any]:
        """Get the default CH type, codec, and nullable for a primitive."""
        if primitive not in self._primitives:
            raise UnknownPrimitiveError(f"Unknown primitive '{primitive}'")
        return dict(self._primitives[primitive])
