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
    from dfe_engine.settings import DFESettings


@dataclass
class ServicePlugin:
    """Consolidation unit for a DFE service.

    All per-service data lives here. Modules like registry.py, validators.py and
    templates.py look up the plugin instead of branching on service name strings.

    Attributes:
        descriptor: Static service metadata (name, display name, description).
        config_class: Pydantic model for the service runtime config (YAML).
        validate_config: Optional cross-field validation callback.
            Signature: ``(config: BaseModel, errors: list[str], warnings: list[str]) -> None``
        config_template_overrides: Per-profile config overrides for template generation.
            Shape: ``{"production": {"kafka": {"sasl": {...}}}, "k8s": {...}}``
        settings_overrides: Overrides that follow the engine's settings, built each time
            a template is generated and merged over ``config_template_overrides``.
            Signature: ``(profile: str, settings: DFESettings) -> dict``
    """

    descriptor: ServiceDescriptor
    config_class: type[BaseModel]
    validate_config: Callable[..., None] | None = None
    config_template_overrides: dict[str, dict[str, Any]] = field(default_factory=dict)
    settings_overrides: Callable[[str, DFESettings], dict[str, Any]] | None = None
