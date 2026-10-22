"""Default configuration template generation for DFE services.

Generates deployment-ready configuration templates for different profiles.
Template overrides are sourced from the plugin system.
"""

import copy
from typing import Any

from dfe_engine.services.plugins import get_plugin, valid_services
from dfe_engine.settings import DFESettings, get_settings
from dfe_engine.yaml_utils import deep_merge

PROFILES = frozenset({"default", "production", "k8s"})


def profile_overrides(
    service: str, profile: str, settings: DFESettings | None = None
) -> dict[str, Any]:
    """The keys *profile* sets over the service's own defaults, with nothing else.

    A plugin's ``settings_overrides`` read *settings*, the process's own when None, so
    the receiver's production bearer listeners point at the secrets backend
    ``DFE_SECRETS_PROVIDER`` selects.

    Args:
        service: Service name (e.g. 'receiver', 'loader', 'archiver').
        profile: One of :data:`PROFILES`. 'default' sets nothing.
        settings: The engine settings to build from, the process's own when None.

    Returns:
        The profile's keys, as a mapping to merge over the service's defaults.

    Raises:
        ValueError: Unknown service or profile, or a profile whose settings override
            names something the service cannot read, such as receiver bearer tokens on
            a secrets backend dfe-receiver has no provider for.
    """
    services = valid_services()
    if service not in services:
        msg = f"Unknown service: {service}. Valid: {', '.join(sorted(services))}"
        raise ValueError(msg)
    if profile not in PROFILES:
        msg = f"Unknown profile: {profile}. Valid: {', '.join(sorted(PROFILES))}"
        raise ValueError(msg)
    if profile == "default":
        return {}

    plugin = get_plugin(service)
    overrides = copy.deepcopy(plugin.config_template_overrides.get(profile, {}))
    if plugin.settings_overrides is not None:
        engine = settings if settings is not None else get_settings()
        deep_merge(overrides, plugin.settings_overrides(profile, engine))
    return overrides


def generate_template(
    service: str, profile: str = "default", settings: DFESettings | None = None
) -> dict[str, Any]:
    """Generate a configuration template for a service.

    The service's defaults with :func:`profile_overrides` merged over them.

    Args:
        service: Service name (e.g. 'receiver', 'loader', 'archiver')
        profile: Configuration profile
            - 'default': Rust defaults (dev-friendly, localhost)
            - 'production': Production-hardened (TLS, SASL, tuned buffers)
            - 'k8s': listeners bound for a pod, and empty broker and host lists
              for the deployment to fill with its own
        settings: The engine settings a plugin's ``settings_overrides`` read, the
            process's own when None.

    Returns:
        Configuration dictionary ready for YAML serialization

    Raises:
        ValueError: As :func:`profile_overrides` raises.
    """
    overrides = profile_overrides(service, profile, settings)
    base = get_plugin(service).config_class().model_dump(mode="json")
    # A profile's list replaces the app's default list rather than extending it.
    if overrides:
        deep_merge(base, overrides)
    return base
