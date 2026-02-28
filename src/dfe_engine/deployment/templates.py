"""Default deployment template generation for DFE services.

Generates deployment-ready configuration templates for different profiles.
Uses the plugin system for per-service defaults.
"""

from __future__ import annotations

from typing import Any

from dfe_engine.deployment.models.common import TShirtSize
from dfe_engine.deployment.sizing import get_keda_defaults, get_default_size, get_resources
from dfe_engine.services.plugins import deployment_classes, valid_services


def generate_template(service: str, profile: str = "default") -> dict[str, Any]:
    """Generate a deployment configuration template for a service.

    Args:
        service: Service name
        profile: Configuration profile
            - 'default': Dev-friendly (KEDA disabled, xs sizing, 1 replica)
            - 'production': Production (KEDA enabled, service default sizing)

    Returns:
        Configuration dictionary ready for YAML serialization

    Raises:
        ValueError: Unknown service or profile
    """
    services = valid_services()
    if service not in services:
        msg = f"Unknown service: {service}. Valid: {', '.join(sorted(services))}"
        raise ValueError(msg)

    deploy_cls = deployment_classes().get(service)
    if deploy_cls is None:
        msg = f"No deployment config class registered for: {service}"
        raise ValueError(msg)

    config = deploy_cls()
    base = config.model_dump(mode="json")

    if profile == "default":
        return _apply_default_overrides(service, base)
    elif profile == "production":
        return _apply_production_overrides(service, base)
    else:
        msg = f"Unknown profile: {profile}. Valid: default, production"
        raise ValueError(msg)


def _apply_default_overrides(service: str, config: dict) -> dict:
    """Apply dev-friendly defaults: KEDA off, xs sizing, 1 replica."""
    config["size"] = TShirtSize.xs.value
    config["replicas"] = 1

    resources = get_resources(TShirtSize.xs)
    config["resources"] = resources.model_dump()

    config["keda"]["enabled"] = False
    config["hpa"]["enabled"] = False

    return config


def _apply_production_overrides(service: str, config: dict) -> dict:
    """Apply production defaults: KEDA on, service-default sizing, prometheus annotations."""
    default_size = get_default_size(service)
    config["size"] = default_size.value

    resources = get_resources(default_size)
    config["resources"] = resources.model_dump()

    config["keda"]["enabled"] = True
    keda_defaults = get_keda_defaults(service)
    if keda_defaults:
        config["keda"]["min_replicas"] = keda_defaults.get(
            "min_replicas", config["keda"]["min_replicas"]
        )
        config["keda"]["max_replicas"] = keda_defaults.get(
            "max_replicas", config["keda"]["max_replicas"]
        )
        if "kafka_trigger" in keda_defaults:
            config["keda"]["kafka_trigger"] = keda_defaults["kafka_trigger"].copy()

    config["hpa"]["enabled"] = False

    config["pod"]["annotations"] = {
        "prometheus.io/scrape": "true",
        "prometheus.io/port": str(config["service"]["metrics_port"]),
        "prometheus.io/path": "/metrics",
    }

    return config
