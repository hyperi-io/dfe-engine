"""Engine Registry — canonical set of permitted ClickHouse table engines.

Restricts the table engine a source may use to a predefined, validated set,
mirroring how TypeRegistry restricts column primitives. The registry lists the
MergeTree-family VARIANTS (MergeTree, ReplacingMergeTree, SummingMergeTree, ...);
validation gates the variant, so a parameterised form like
``ReplacingMergeTree(version_col)`` is permitted - the variant must be in the set,
the params inside the parens are the caller's. The Replicated/Shared prefix is NOT
listed: it is added at DDL time by the engine resolver from the topology, never
declared in config.

Both the Source model (SourceSchema.engine) and the DDL generation path
(DDLGenerator) validate through this single registry, so the permitted set
lives in one place and cannot drift.

Usage:
    from dfe_engine.source.engine_registry import EngineRegistry

    registry = EngineRegistry.default()
    registry.validate("MergeTree")   # ok
    registry.validate("Log")         # raises InvalidEngineError
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from dfe_engine.yaml_utils import yaml_load


class EngineRegistryError(Exception):
    """Base exception for engine registry errors."""


class InvalidEngineError(EngineRegistryError):
    """Engine is not in the permitted set."""


class EngineRegistry:
    """Canonical registry of permitted ClickHouse table engines.

    Loaded from engine_registry.yaml. Provides validate() and an engines
    listing, mirroring TypeRegistry's validation surface.
    """

    def __init__(self, data: dict[str, Any]) -> None:
        self._engines: set[str] = set(data["engines"])

    @classmethod
    def default(cls) -> EngineRegistry:
        """Load the default engine registry from the package resource."""
        yaml_path = Path(__file__).parent / "engine_registry.yaml"
        data = yaml_load(yaml_path)
        return cls(data)

    @classmethod
    def from_file(cls, path: str | Path) -> EngineRegistry:
        """Load an engine registry from a custom YAML file."""
        data = yaml_load(path)
        return cls(data)

    # -----------------------------------------------------------------
    # Validation
    # -----------------------------------------------------------------

    def validate(self, engine: str) -> None:
        """Validate an engine's VARIANT against the permitted set.

        Accepts a parameterised form: the family variant (the token before the
        first ``(``) is what must be in the registry, so ``ReplacingMergeTree(ver)``
        validates on its variant ``ReplacingMergeTree``. The params inside the
        parens (a version column, summing columns, ...) are the caller's - the
        registry gates the family, not the arguments.

        Raises:
            InvalidEngineError: The variant is not in the registry.
        """
        variant = engine.split("(", 1)[0].strip()
        if variant not in self._engines:
            raise InvalidEngineError(
                f"Invalid engine {engine!r}. Valid variants: {', '.join(self.engines)}"
            )

    # -----------------------------------------------------------------
    # Introspection
    # -----------------------------------------------------------------

    @property
    def engines(self) -> list[str]:
        """List all permitted engine names."""
        return sorted(self._engines)
