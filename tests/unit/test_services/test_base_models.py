"""Tests for the base service config model and the service descriptor."""

import pytest

from dfe_engine.services.descriptor import KafkaRole, ServiceDescriptor
from dfe_engine.services.models.base import BaseServiceConfig


class TestBaseServiceConfig:
    """Test BaseServiceConfig — the abstract base for all service configs."""

    def test_default_metrics(self):
        cfg = BaseServiceConfig()
        assert cfg.metrics.enabled is True
        assert cfg.metrics.address == "0.0.0.0:9090"

    def test_default_logging(self):
        cfg = BaseServiceConfig()
        assert cfg.logging.level == "info"
        assert cfg.logging.format == "json"

    def test_inherits_properly(self):
        """All 6 service configs should inherit from BaseServiceConfig."""
        from dfe_engine.services.models.archiver import ArchiverConfig
        from dfe_engine.services.models.fetcher import FetcherConfig
        from dfe_engine.services.models.loader import LoaderConfig
        from dfe_engine.services.models.receiver import ReceiverConfig
        from dfe_engine.services.models.transform_vector import TransformVectorConfig
        from dfe_engine.services.models.transform_wasm import TransformWasmConfig

        for cls in [
            ReceiverConfig,
            LoaderConfig,
            ArchiverConfig,
            TransformVectorConfig,
            TransformWasmConfig,
            FetcherConfig,
        ]:
            assert issubclass(cls, BaseServiceConfig), (
                f"{cls.__name__} must inherit BaseServiceConfig"
            )


class TestServiceDescriptor:
    """Test ServiceDescriptor frozen dataclass."""

    def test_create_descriptor(self):
        d = ServiceDescriptor(
            name="test",
            display_name="Test Service",
            image="test:latest",
        )
        assert d.name == "test"
        assert d.default_port == 8080
        assert d.metrics_port == 9090
        assert d.kafka_role == KafkaRole.NONE

    def test_descriptor_is_frozen(self):
        d = ServiceDescriptor(name="test", display_name="Test", image="test:latest")
        with pytest.raises(AttributeError):
            d.name = "changed"

    def test_kafka_role_enum(self):
        assert KafkaRole.PRODUCER.value == "producer"
        assert KafkaRole.CONSUMER.value == "consumer"
        assert KafkaRole.BOTH.value == "both"
        assert KafkaRole.NONE.value == "none"

    def test_extra_ports(self):
        d = ServiceDescriptor(
            name="test",
            display_name="Test",
            image="test:latest",
            extra_ports={"grpc": 6000, "admin": 9091},
        )
        assert d.extra_ports["grpc"] == 6000
