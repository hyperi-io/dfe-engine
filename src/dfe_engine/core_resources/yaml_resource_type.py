#  Purpose:      Read resource_type from on-disk YAML config dicts
#  License:      BUSL-1.1

from __future__ import annotations

from typing import Any, Literal

ResourceType = Literal["core", "custom"]

CORE_RESOURCE_MUTATION_MESSAGE = "Core resources can't be mutated"


def resource_type_from_config(config: dict[str, Any] | None) -> ResourceType:
    """Return ``resource_type`` from a loaded YAML document (default ``custom``)."""
    if not config:
        return "custom"
    raw = config.get("resource_type")
    if raw is None and "resource_type" in config:
        raw = config.get("resource_type")
    if raw == "core":
        return "core"
    return "custom"


def config_is_core(config: dict[str, Any] | None) -> bool:
    return resource_type_from_config(config) == "core"
