"""Default configuration template generation for DFE services.

Generates deployment-ready configuration templates for different profiles.
Template overrides are sourced from the plugin system.
"""

import copy
from typing import Any

from dfe_engine.services.plugins import get_plugin, valid_services
from dfe_engine.yaml_utils import deep_merge


def generate_template(service: str, profile: str = "default") -> dict[str, Any]:
    """Generate a configuration template for a service.

    Args:
        service: Service name (e.g. 'receiver', 'loader', 'archiver')
        profile: Configuration profile
            - 'default': Rust defaults (dev-friendly, localhost)
            - 'production': Production-hardened (TLS, SASL, tuned buffers)
            - 'k8s': listeners bound for a pod, and empty broker and host lists
              for the deployment to fill with its own

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

    # A profile's list replaces the app's default list rather than extending it.
    overrides = plugin.config_template_overrides.get(profile, {})
    if overrides:
        merged = copy.deepcopy(base)
        deep_merge(merged, overrides, replace_lists=True)
        return merged

    return base
