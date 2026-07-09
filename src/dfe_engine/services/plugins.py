"""Plugin registry for DFE services.

Discovers and manages ServicePlugin instances. Built-in plugins are registered
via ``importlib.metadata`` entry_points (group ``dfe_engine.services``).
External packages can register additional services the same way.

Usage:
    from dfe_engine.services.plugins import get_plugin, valid_services

    plugin = get_plugin("receiver")
    print(plugin.descriptor.image)

    all_services = valid_services()  # {"receiver", "loader", "archiver", ...}
"""

from __future__ import annotations

import importlib.metadata

from pydantic import BaseModel

from dfe_engine.services.plugin import ServicePlugin

# Internal registry — populated on first access
_plugins: dict[str, ServicePlugin] | None = None

# Entry point group name
_EP_GROUP = "dfe_engine.services"


def _discover() -> dict[str, ServicePlugin]:
    """Discover plugins from entry_points and return the registry dict."""
    registry: dict[str, ServicePlugin] = {}

    eps = importlib.metadata.entry_points()
    group_eps = eps.select(group=_EP_GROUP) if hasattr(eps, "select") else eps.get(_EP_GROUP, [])

    for ep in group_eps:
        try:
            plugin = ep.load()
            if callable(plugin) and not isinstance(plugin, ServicePlugin):
                plugin = plugin()
            if isinstance(plugin, ServicePlugin):
                registry[ep.name] = plugin
        except Exception as exc:
            import warnings

            warnings.warn(
                f"Failed to load service plugin '{ep.name}': {exc}",
                RuntimeWarning,
                stacklevel=2,
            )

    return registry


def _ensure_loaded() -> dict[str, ServicePlugin]:
    """Lazy-load plugins on first access."""
    global _plugins
    if _plugins is None:
        _plugins = _discover()
    return _plugins


def register(name: str, plugin: ServicePlugin) -> None:
    """Programmatically register a plugin (useful for tests and ad-hoc plugins).

    Args:
        name: Service name (e.g. "receiver").
        plugin: ServicePlugin instance.
    """
    registry = _ensure_loaded()
    registry[name] = plugin


def get_plugin(name: str) -> ServicePlugin:
    """Get a registered plugin by service name.

    Args:
        name: Service name.

    Raises:
        KeyError: If no plugin is registered for the name.
    """
    registry = _ensure_loaded()
    if name not in registry:
        available = ", ".join(sorted(registry.keys()))
        raise KeyError(f"Unknown service: {name}. Available: {available}")
    return registry[name]


def valid_services() -> set[str]:
    """Return the set of all registered service names."""
    return set(_ensure_loaded().keys())


def config_classes() -> dict[str, type[BaseModel]]:
    """Return service name -> config class mapping (replaces SERVICE_CONFIG_CLASSES)."""
    return {name: p.config_class for name, p in _ensure_loaded().items()}


def deployment_classes() -> dict[str, type[BaseModel]]:
    """Return service name -> deployment config class mapping."""
    out: dict[str, type[BaseModel]] = {}
    for name, p in _ensure_loaded().items():
        cls = p.deployment_class
        if cls is not None:
            out[name] = cls
    return out


def all_plugins() -> dict[str, ServicePlugin]:
    """Return a copy of the full plugin registry."""
    return dict(_ensure_loaded())


def reset() -> None:
    """Reset the registry (for test isolation). Forces re-discovery on next access."""
    global _plugins
    _plugins = None
