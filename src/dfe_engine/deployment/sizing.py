"""Unified t-shirt sizing for DFE service deployments.

Provides a single resource table (2 GiB : 1 CPU ratio, C-series aligned)
shared by all services. Per-service differences are sourced from the plugin
system (KEDA thresholds, service config overrides, default sizes).

The ``apply_sizing`` function is the bridge between deployment config and
service config: it returns both K8s resource overrides and matching service
config overrides so that buffer sizes, memory limits, etc. scale with the pod.
"""

from __future__ import annotations

from typing import Any

from dfe_engine.deployment.models.common import (
    ResourceQuantity,
    ResourceSpec,
    TShirtSize,
)

# ---------------------------------------------------------------------------
# Unified resource table — all services share this
# ---------------------------------------------------------------------------

RESOURCE_SIZES: dict[str, ResourceSpec] = {
    "xs": ResourceSpec(
        requests=ResourceQuantity(cpu="250m", memory="512Mi"),
        limits=ResourceQuantity(cpu="500m", memory="1Gi"),
    ),
    "small": ResourceSpec(
        requests=ResourceQuantity(cpu="500m", memory="1Gi"),
        limits=ResourceQuantity(cpu="1", memory="2Gi"),
    ),
    "medium": ResourceSpec(
        requests=ResourceQuantity(cpu="1", memory="2Gi"),
        limits=ResourceQuantity(cpu="2", memory="4Gi"),
    ),
    "large": ResourceSpec(
        requests=ResourceQuantity(cpu="2", memory="4Gi"),
        limits=ResourceQuantity(cpu="4", memory="8Gi"),
    ),
    "xlarge": ResourceSpec(
        requests=ResourceQuantity(cpu="4", memory="8Gi"),
        limits=ResourceQuantity(cpu="8", memory="16Gi"),
    ),
}


def get_resources(size: TShirtSize | str) -> ResourceSpec:
    """Get the resource spec for a t-shirt size.

    Args:
        size: T-shirt size (xs, small, medium, large, xlarge)

    Returns:
        ResourceSpec with requests and limits

    Raises:
        ValueError: If size is 'custom' (caller must provide resources)
                    or unknown size
    """
    size_str = size.value if isinstance(size, TShirtSize) else size
    if size_str == "custom":
        msg = "Size 'custom' requires explicit resources — use resources field directly"
        raise ValueError(msg)
    if size_str not in RESOURCE_SIZES:
        msg = f"Unknown size: {size_str}. Valid: {', '.join(sorted(RESOURCE_SIZES))}"
        raise ValueError(msg)
    return RESOURCE_SIZES[size_str]


def get_service_overrides(service: str, size: TShirtSize | str) -> dict[str, Any]:
    """Get service config overrides for a service at a given size.

    Sourced from the plugin's sizing_overrides.
    """
    from dfe_engine.services.plugins import get_plugin

    size_str = size.value if isinstance(size, TShirtSize) else size
    if size_str == "custom":
        return {}

    try:
        plugin = get_plugin(service)
        return plugin.sizing_overrides.get(size_str, {})
    except KeyError:
        return {}


def get_keda_defaults(service: str) -> dict[str, Any]:
    """Get default KEDA configuration for a service from its plugin."""
    from dfe_engine.services.plugins import get_plugin

    try:
        plugin = get_plugin(service)
        return plugin.keda_defaults.copy()
    except KeyError:
        return {}


def get_default_size(service: str) -> TShirtSize:
    """Get default t-shirt size for a service from its plugin."""
    from dfe_engine.services.plugins import get_plugin

    try:
        plugin = get_plugin(service)
        return TShirtSize(plugin.default_size)
    except (KeyError, ValueError):
        return TShirtSize.small


# Backward-compatible module-level dicts (lazy-built from plugins)
_compat_init_done = False
KEDA_DEFAULTS: dict[str, dict[str, Any]] = {}
DEFAULT_SIZES: dict[str, TShirtSize] = {}


def _ensure_compat_dicts() -> None:
    """Populate backward-compatible dicts from plugins (once)."""
    global _compat_init_done
    if _compat_init_done:
        return
    _compat_init_done = True

    from dfe_engine.services.plugins import all_plugins

    for name, p in all_plugins().items():
        if p.keda_defaults:
            KEDA_DEFAULTS[name] = p.keda_defaults.copy()
        try:
            DEFAULT_SIZES[name] = TShirtSize(p.default_size)
        except ValueError:
            DEFAULT_SIZES[name] = TShirtSize.small


def apply_sizing(
    service: str, size: TShirtSize | str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Apply t-shirt sizing to get both deployment and service config overrides.

    Args:
        service: Service name
        size: T-shirt size

    Returns:
        Tuple of (deployment_overrides, service_config_overrides).

    Raises:
        ValueError: Unknown service or size='custom' without explicit resources
    """
    _ensure_compat_dicts()
    from dfe_engine.services.plugins import valid_services

    services = valid_services()
    if service not in services:
        msg = f"Unknown service: {service}. Valid: {', '.join(sorted(services))}"
        raise ValueError(msg)

    resources = get_resources(size)
    deploy_overrides: dict[str, Any] = {
        "resources": resources.model_dump(),
    }

    keda = get_keda_defaults(service)
    if keda:
        deploy_overrides["keda"] = keda

    service_overrides = get_service_overrides(service, size)

    return deploy_overrides, service_overrides
