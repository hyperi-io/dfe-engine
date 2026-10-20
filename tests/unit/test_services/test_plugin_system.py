"""Tests for the service plugin discovery and registry system."""

import pytest

from dfe_engine.services.descriptor import ServiceDescriptor
from dfe_engine.services.plugin import ServicePlugin
from dfe_engine.services.plugins import (
    all_plugins,
    config_classes,
    get_plugin,
    register,
    reset,
    valid_services,
)


@pytest.fixture(autouse=True)
def _reset_plugins():
    """Reset plugin registry before each test to ensure isolation."""
    reset()
    yield
    reset()


class TestPluginDiscovery:
    """Test that entry_point-based discovery finds all 7 built-in plugins."""

    def test_discovers_all_builtin_services(self):
        services = valid_services()
        expected = {
            "receiver",
            "loader",
            "archiver",
            "transform-vector",
            "transform-vrl",
            "transform-wasm",
            "fetcher",
        }
        assert services == expected

    def test_get_plugin_returns_correct_type(self):
        for name in valid_services():
            plugin = get_plugin(name)
            assert isinstance(plugin, ServicePlugin)

    def test_get_plugin_unknown_raises(self):
        with pytest.raises(KeyError, match="Unknown service"):
            get_plugin("nonexistent-service")

    def test_all_plugins_returns_dict(self):
        plugins = all_plugins()
        assert isinstance(plugins, dict)
        assert len(plugins) == 7

    def test_config_classes_mapping(self):
        classes = config_classes()
        assert len(classes) == 7
        for name, cls in classes.items():
            assert name in valid_services()
            assert hasattr(cls, "model_validate")


class TestPluginRegistration:
    """Test programmatic plugin registration (for tests and ad-hoc plugins)."""

    def test_register_custom_plugin(self):
        from pydantic import BaseModel

        class DummyConfig(BaseModel):
            value: str = "test"

        desc = ServiceDescriptor(name="custom-test", display_name="Custom Test")
        plugin = ServicePlugin(descriptor=desc, config_class=DummyConfig)
        register("custom-test", plugin)

        assert "custom-test" in valid_services()
        assert get_plugin("custom-test") is plugin

    def test_reset_clears_registry(self):
        # After reset, next access re-discovers from entry points
        services_before = valid_services()
        reset()
        services_after = valid_services()
        assert services_before == services_after


class TestDescriptors:
    """Test that each plugin has a properly configured descriptor."""

    @pytest.mark.parametrize(
        "service",
        [
            "receiver",
            "loader",
            "archiver",
            "transform-vector",
            "transform-wasm",
            "fetcher",
        ],
    )
    def test_descriptor_fields(self, service):
        plugin = get_plugin(service)
        d = plugin.descriptor
        assert d.name == service
        assert d.display_name
        assert d.description
