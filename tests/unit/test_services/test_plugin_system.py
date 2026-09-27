"""Tests for the service plugin discovery and registry system."""

import pytest

from dfe_engine.services.descriptor import KafkaRole, ServiceDescriptor
from dfe_engine.services.plugin import ServicePlugin
from dfe_engine.services.plugins import (
    all_plugins,
    config_classes,
    deployment_classes,
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

    def test_deployment_classes_mapping(self):
        classes = deployment_classes()
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

        desc = ServiceDescriptor(
            name="custom-test",
            display_name="Custom Test",
            image="test:latest",
        )
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
        assert d.image == f"ghcr.io/hyperi-io/dfe-{service}"
        assert d.default_port > 0
        assert d.metrics_port > 0
        assert isinstance(d.kafka_role, KafkaRole)

    def test_receiver_is_producer(self):
        assert get_plugin("receiver").descriptor.kafka_role == KafkaRole.PRODUCER

    def test_loader_is_consumer(self):
        assert get_plugin("loader").descriptor.kafka_role == KafkaRole.CONSUMER

    def test_archiver_is_consumer(self):
        assert get_plugin("archiver").descriptor.kafka_role == KafkaRole.CONSUMER

    def test_transforms_are_both(self):
        assert get_plugin("transform-vector").descriptor.kafka_role == KafkaRole.BOTH
        assert get_plugin("transform-wasm").descriptor.kafka_role == KafkaRole.BOTH

    def test_fetcher_is_producer(self):
        assert get_plugin("fetcher").descriptor.kafka_role == KafkaRole.PRODUCER

    def test_receiver_has_grpc_port(self):
        d = get_plugin("receiver").descriptor
        assert "grpc" in d.extra_ports
        assert d.extra_ports["grpc"] == 6000


class TestPluginDefaults:
    """Test default_size and keda_defaults per plugin."""

    def test_receiver_default_size(self):
        assert get_plugin("receiver").default_size == "small"

    def test_loader_default_size(self):
        assert get_plugin("loader").default_size == "medium"

    def test_transform_vector_default_size(self):
        assert get_plugin("transform-vector").default_size == "medium"

    def test_transform_wasm_default_size(self):
        assert get_plugin("transform-wasm").default_size == "medium"

    def test_fetcher_default_size(self):
        assert get_plugin("fetcher").default_size == "small"

    def test_keda_defaults_present(self):
        for name in valid_services():
            plugin = get_plugin(name)
            assert isinstance(plugin.keda_defaults, dict)
