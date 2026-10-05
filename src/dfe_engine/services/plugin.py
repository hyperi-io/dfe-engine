"""Service plugin definition.

A ServicePlugin bundles all per-service knowledge into a single unit:
descriptor, config class, validation, and templates.

Each service registers one plugin. The plugin system replaces hardcoded
if/elif chains scattered across validators.py, templates.py, etc.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pydantic import BaseModel

    from dfe_engine.services.descriptor import ServiceDescriptor


@dataclass
class ServicePlugin:
    """Consolidation unit for a DFE service.

    All per-service data lives here. Modules like registry.py, validators.py and
    templates.py look up the plugin instead of branching on service name strings.

    Attributes:
        descriptor: Static service metadata (name, image, ports, health paths).
        config_class: Pydantic model for the service runtime config (YAML).
        validate_config: Optional cross-field validation callback.
            Signature: ``(config: BaseModel, errors: list[str], warnings: list[str]) -> None``
        config_template_overrides: Per-profile config overrides for template generation.
            Shape: ``{"production": {"kafka": {"sasl": {...}}}, "k8s": {...}}``
    """

    descriptor: ServiceDescriptor
    config_class: type[BaseModel]
    validate_config: Callable[..., None] | None = None
    config_template_overrides: dict[str, dict[str, Any]] = field(default_factory=dict)
