"""Default configuration template generation for DFE services.

Generates deployment-ready configuration templates for different profiles.
Template overrides are sourced from the plugin system.
"""

import copy
from typing import Any

from dfe_engine.services.plugins import get_plugin, valid_services
from dfe_engine.settings import get_settings
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

    A plugin's ``settings_overrides`` read the engine's own settings, so a template
    names what this engine writes: the receiver's production bearer listeners point
    at the secrets backend ``DFE_SECRETS_PROVIDER`` selects.

    Returns:
        Configuration dictionary ready for YAML serialization

    Raises:
        ValueError: Unknown service or profile, or a profile whose settings override
            names something the service cannot read, such as receiver bearer tokens on
            a secrets backend dfe-receiver has no provider for.
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
    overrides = copy.deepcopy(plugin.config_template_overrides.get(profile, {}))
    if plugin.settings_overrides is not None:
        deep_merge(overrides, plugin.settings_overrides(profile, get_settings()))
    if overrides:
        merged = copy.deepcopy(base)
        deep_merge(merged, overrides)
        return merged

    return base
