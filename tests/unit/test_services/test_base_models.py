"""Tests for the base service config model and the service descriptor."""

import pytest

from dfe_engine.services.descriptor import ServiceDescriptor
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
        d = ServiceDescriptor(name="test", display_name="Test Service")
        assert d.name == "test"
        assert d.description == ""

    def test_descriptor_is_frozen(self):
        d = ServiceDescriptor(name="test", display_name="Test")
        with pytest.raises(AttributeError):
            d.name = "changed"  # ty: ignore[invalid-assignment]  # the refused write is the point of the test
