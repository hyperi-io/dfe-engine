"""Engine Registry - the permitted ClickHouse table engines.

Restricts the table engine a source may use to the set dfe-schemas ships as
``registries/engines.yaml``, mirroring how TypeRegistry restricts column
primitives. The registry lists MergeTree-family VARIANTS; the Replicated/Shared
prefix is added at DDL time by the engine resolver from the topology and is
never declared in config.

Two gates: :meth:`EngineRegistry.validate` checks the variant and runs on every
model load, and :meth:`EngineRegistry.validate_arguments` checks each variant's
argument rule and runs when a source is saved, so a stored source written before
the rule existed still loads.

Usage:
    from dfe_engine.source.engine_registry import EngineRegistry

    registry = EngineRegistry.default()
    registry.validate("MergeTree")   # ok
    registry.validate("Log")         # raises InvalidEngineError
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, get_args

from dfe_engine.yaml_utils import yaml_load

ArgumentRule = Literal["none", "optional", "required"]

# The whole engine string: a variant, then optionally one parenthesised argument list with no nested parentheses.
_ENGINE_RE = re.compile(r"\s*(\w+)\s*(?:\(([^()]*)\))?\s*")


class EngineRegistryError(Exception):
    """Base exception for engine registry errors."""


class InvalidEngineError(EngineRegistryError):
    """Engine is not in the permitted set, or breaks its variant's argument rule."""


@dataclass(frozen=True)
class EngineOption:
    """One selectable engine variant and whether it takes arguments."""

    name: str
    description: str
    arguments: ArgumentRule
    argument_hint: str


def _option(*, entry: Any) -> EngineOption:
    """One registry entry as an option, refusing an entry the registry format does not allow."""
    if not (isinstance(entry, dict)) or not (entry.get("name")):
        raise EngineRegistryError(f"engine registry entry has no name: {entry!r}")
    rule = entry.get("arguments")
    if rule not in get_args(ArgumentRule):
        raise EngineRegistryError(
            f"engine {entry['name']!r}: arguments must be one of {get_args(ArgumentRule)}, "
            f"got {rule!r}"
        )
    return EngineOption(
        argument_hint=entry.get("argument_hint") or "",
        arguments=rule,
        description=entry.get("description") or "",
        name=entry["name"],
    )


def _split(engine: str) -> tuple[str, str]:
    """The variant and the text inside its parentheses, e.g. ("ReplacingMergeTree", "ver")."""
    variant, paren, rest = engine.partition("(")
    arguments = rest.rsplit(")", 1)[0].strip() if paren else ""
    return variant.strip(), arguments


class EngineRegistry:
    """Canonical registry of permitted ClickHouse table engines."""

    def __init__(self, data: dict[str, Any]) -> None:
        engines = data.get("engines") if isinstance(data, dict) else None
        if not (isinstance(engines, list)) or not (engines):
            raise EngineRegistryError("engine registry must list its engines under 'engines'")
        self._options = {}
        for entry in engines:
            option = _option(entry=entry)
            self._options[option.name] = option

    @classmethod
    def default(cls) -> EngineRegistry:
        """Load the engine registry dfe-schemas ships as ``registries/engines.yaml``."""
        from dfe_engine.schema.schema_loader import resolve_registry_path

        return cls(yaml_load(resolve_registry_path("engines.yaml")))

    @classmethod
    def from_file(cls, path: str | Path) -> EngineRegistry:
        """Load an engine registry from a custom YAML file."""
        return cls(yaml_load(path))

    # -----------------------------------------------------------------
    # Validation
    # -----------------------------------------------------------------

    def validate(self, engine: str) -> None:
        """Validate an engine's VARIANT against the permitted set.

        The variant is the token before the first ``(``; what is inside the
        parentheses is checked by :meth:`validate_arguments`, not here.

        Raises:
            InvalidEngineError: The variant is not in the registry.
        """
        variant, _ = _split(engine)
        if variant not in self._options:
            raise InvalidEngineError(
                f"Invalid engine {engine!r}. Valid variants: {', '.join(self.engines)}"
            )

    def validate_arguments(self, engine: str) -> None:
        """Validate *engine* against its variant's argument rule.

        Raises:
            InvalidEngineError: The string is not one variant with at most one
                argument list, the variant is unknown, takes no arguments but was
                given some, or requires arguments and was given none.
        """
        match = _ENGINE_RE.fullmatch(engine)
        if match is None:
            raise InvalidEngineError(
                f"Invalid engine {engine!r}: expected Variant or Variant(arguments) and nothing else"
            )
        self.validate(engine)
        variant, arguments = match.group(1), (match.group(2) or "").strip()
        option = self._options[variant]
        if option.arguments == "none" and arguments:
            raise InvalidEngineError(f"Engine {variant!r} takes no arguments; got {engine!r}")
        if option.arguments == "required" and not (arguments):
            raise InvalidEngineError(
                f"Engine {variant!r} requires arguments ({option.argument_hint}); got {engine!r}"
            )

    # -----------------------------------------------------------------
    # Introspection
    # -----------------------------------------------------------------

    @property
    def engines(self) -> list[str]:
        """List all permitted engine names, sorted."""
        return sorted(self._options)

    @property
    def options(self) -> list[EngineOption]:
        """Every selectable engine, in the order the registry lists them."""
        return list(self._options.values())
