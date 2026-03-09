"""Tests for base service and deployment config models."""

import pytest

from dfe_engine.services.models.base import BaseServiceConfig
from dfe_engine.services.descriptor import KafkaRole, ServiceDescriptor
from dfe_engine.deployment.models.common import (
    BaseDeploymentConfig,
    TShirtSize,
    ResourceSpec,
    ResourceQuantity,
)


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
        from dfe_engine.services.models.receiver import ReceiverConfig
        from dfe_engine.services.models.loader import LoaderConfig
        from dfe_engine.services.models.archiver import ArchiverConfig
        from dfe_engine.services.models.transform_vector import TransformVectorConfig
        from dfe_engine.services.models.transform_wasm import TransformWasmConfig
        from dfe_engine.services.models.fetcher import FetcherConfig

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


class TestBaseDeploymentConfig:
    """Test BaseDeploymentConfig — the abstract base for deployment configs."""

    def test_defaults(self):
        cfg = BaseDeploymentConfig()
        assert cfg.size == TShirtSize.small
        assert cfg.replicas == 1
        assert cfg.resources is None
        assert cfg.keda.enabled is False
        assert cfg.hpa.enabled is False

    def test_custom_resources(self):
        cfg = BaseDeploymentConfig(
            size=TShirtSize.custom,
            resources=ResourceSpec(
                requests=ResourceQuantity(cpu="500m", memory="1Gi"),
                limits=ResourceQuantity(cpu="1", memory="2Gi"),
            ),
        )
        assert cfg.resources.requests.cpu == "500m"
        assert cfg.resources.limits.memory == "2Gi"

    def test_inherits_properly(self):
        """All 6 deployment configs should inherit from BaseDeploymentConfig."""
        from dfe_engine.deployment.models.receiver import ReceiverDeploymentConfig
        from dfe_engine.deployment.models.loader import LoaderDeploymentConfig
        from dfe_engine.deployment.models.archiver import ArchiverDeploymentConfig
        from dfe_engine.deployment.models.transform_vector import TransformVectorDeploymentConfig
        from dfe_engine.deployment.models.transform_wasm import TransformWasmDeploymentConfig
        from dfe_engine.deployment.models.fetcher import FetcherDeploymentConfig

        for cls in [
            ReceiverDeploymentConfig,
            LoaderDeploymentConfig,
            ArchiverDeploymentConfig,
            TransformVectorDeploymentConfig,
            TransformWasmDeploymentConfig,
            FetcherDeploymentConfig,
        ]:
            assert issubclass(cls, BaseDeploymentConfig), (
                f"{cls.__name__} must inherit BaseDeploymentConfig"
            )

    def test_tshirt_sizes(self):
        for size in TShirtSize:
            cfg = BaseDeploymentConfig(size=size)
            assert cfg.size == size
