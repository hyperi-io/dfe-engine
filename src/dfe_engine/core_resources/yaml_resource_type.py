#  Purpose:      Read resource_type from on-disk YAML config dicts
#  License:      BUSL-1.1

from __future__ import annotations

from typing import Any, Literal

ResourceType = Literal["core", "custom"]

CORE_RESOURCE_MUTATION_MESSAGE = "Core resources can't be mutated"


def resource_type_from_config(config: dict[str, Any] | None) -> ResourceType:
    """Return ``resource_type`` from a loaded YAML document (default ``custom``)."""
    if not (config):
        return "custom"
    return "core" if config.get("resource_type") == "core" else "custom"


def config_is_core(config: dict[str, Any] | None) -> bool:
    return resource_type_from_config(config) == "core"
