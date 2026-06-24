"""Engine Registry — canonical set of permitted ClickHouse table engines.

Restricts the table engine a source may use to a predefined, validated set,
mirroring how TypeRegistry restricts column primitives. Engines are emitted
bare (``ENGINE = Name()``), so only parameterless MergeTree-family engines
belong in the registry.

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
        """Validate that an engine is in the permitted set.

        Raises:
            InvalidEngineError: Engine not in the registry.
        """
        if engine not in self._engines:
            raise InvalidEngineError(f"Invalid engine {engine!r}. Valid: {', '.join(self.engines)}")

    # -----------------------------------------------------------------
    # Introspection
    # -----------------------------------------------------------------

    @property
    def engines(self) -> list[str]:
        """List all permitted engine names."""
        return sorted(self._engines)
