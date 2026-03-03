"""Default configuration template generation for DFE services.

Generates deployment-ready configuration templates for different profiles.
Template overrides are sourced from the plugin system.
"""

from __future__ import annotations

import copy
from typing import Any

from deepmerge import always_merger

from dfe_engine.services.plugins import get_plugin, valid_services


def generate_template(service: str, profile: str = "default") -> dict[str, Any]:
    """Generate a configuration template for a service.

    Args:
        service: Service name (e.g. 'receiver', 'loader', 'archiver')
        profile: Configuration profile
            - 'default': Rust defaults (dev-friendly, localhost)
            - 'production': Production-hardened (TLS, SASL, tuned buffers)
            - 'k8s': Kubernetes-optimized (service DNS, ConfigMap-friendly)

    Returns:
        Configuration dictionary ready for YAML serialization

    Raises:
        ValueError: Unknown service or profile
    """
    services = valid_services()
    if service not in services:
        msg = f"Unknown service: {service}. Valid: {', '.join(sorted(services))}"
        raise ValueError(msg)

    plugin = get_plugin(service)
    config = plugin.config_class()
    base = config.model_dump(mode="json")

    if profile == "default":
        return base

    valid_profiles = {"default", "production", "k8s"}
    if profile not in valid_profiles:
        msg = f"Unknown profile: {profile}. Valid: {', '.join(sorted(valid_profiles))}"
        raise ValueError(msg)

    # Apply plugin-provided overrides for this profile
    overrides = plugin.config_template_overrides.get(profile, {})
    if overrides:
        merged = copy.deepcopy(base)
        always_merger.merge(merged, overrides)
        return merged

    return base
