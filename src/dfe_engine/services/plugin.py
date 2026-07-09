"""Service plugin definition.

A ServicePlugin bundles all per-service knowledge into a single unit:
descriptor, config class, deployment class, validation, sizing, and templates.

Each service registers one plugin. The plugin system replaces hardcoded
if/elif chains scattered across validators.py, templates.py, sizing.py, etc.
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

    All per-service data lives here. Modules like registry.py, validators.py,
    templates.py, and sizing.py look up the plugin instead of branching on
    service name strings.

    Attributes:
        descriptor: Static service metadata (name, image, ports, health paths).
        config_class: Pydantic model for the service runtime config (YAML).
        deployment_class: Pydantic model for K8s deployment config, or None.
            ``BaseModel`` is a TYPE_CHECKING-only import, so the annotation stays
            lazy and introduces no services/ -> deployment/ runtime cycle.
        validate_config: Optional cross-field validation callback.
            Signature: ``(config: BaseModel, errors: list[str], warnings: list[str]) -> None``
        sizing_overrides: Per t-shirt size service config overrides.
            Shape: ``{"xs": {"buffer": {"flush_bytes": 524288}}, ...}``
        keda_defaults: Default KEDA trigger configuration.
            Shape: ``{"min_replicas": 2, "max_replicas": 10, "kafka_trigger": {...}}``
        default_size: Default t-shirt size for production deployments.
        config_template_overrides: Per-profile config overrides for template generation.
            Shape: ``{"production": {"kafka": {"sasl": {...}}}, "k8s": {...}}``
    """

    descriptor: ServiceDescriptor
    config_class: type[BaseModel]
    deployment_class: type[BaseModel] | None = None
    validate_config: Callable[..., None] | None = None
    sizing_overrides: dict[str, dict[str, Any]] = field(default_factory=dict)
    keda_defaults: dict[str, Any] = field(default_factory=dict)
    default_size: str = "small"
    config_template_overrides: dict[str, dict[str, Any]] = field(default_factory=dict)
