"""Tests for new service config models (transform-vector, transform-wasm, fetcher)."""

import pytest

from dfe_engine.services.models.base import BaseServiceConfig
from dfe_engine.services.models.fetcher import (
    FetcherAuthConfig,
    FetcherConfig,
    FetcherSourceConfig,
)
from dfe_engine.services.models.source_common import BaseSourceConfig, SourceFileConfig
from dfe_engine.services.models.transform_vector import (
    TransformVectorConfig,
    VectorSourceConfig,
)
from dfe_engine.services.models.transform_wasm import (
    TransformWasmConfig,
    WasmSourceConfig,
)


class TestBaseSourceConfig:
    """Test the shared BaseSourceConfig model."""

    def test_source_file_config(self):
        f = SourceFileConfig(path="/data/geo.mmdb", type="mmdb", source="configmap/geo")
        assert f.path == "/data/geo.mmdb"
        assert f.type == "mmdb"
        assert f.reload_interval_secs == 3600

    def test_base_source_has_env_and_files(self):
        src = BaseSourceConfig(
            name="test-source",
            env={"KEY": "value"},
            files=[SourceFileConfig(path="/data/file.csv")],
        )
        assert src.name == "test-source"
        assert len(src.files) == 1
        assert src.env["KEY"] == "value"
        assert src.enabled is True

    def test_base_source_defaults(self):
        src = BaseSourceConfig(name="minimal")
        assert src.env == {}
        assert src.files == []
        assert src.enabled is True
        assert src.description == ""


class TestTransformVectorConfig:
    """Test transform-vector configuration model."""

    def test_inherits_base_service_config(self):
        assert issubclass(TransformVectorConfig, BaseServiceConfig)

    def test_vector_source_inherits_base_source(self):
        assert issubclass(VectorSourceConfig, BaseSourceConfig)

    def test_empty_config(self):
        cfg = TransformVectorConfig()
        assert cfg.sources == []
        assert cfg.extra_yaml_files == []
        assert cfg.extra_env == {}
        assert cfg.ipc.enabled is False
        assert cfg.metrics.enabled is True

    def test_with_sources_and_tree(self):
        cfg = TransformVectorConfig(
            sources=[
                VectorSourceConfig(name="root", config_file="/etc/vector/root.yaml"),
                VectorSourceConfig(
                    name="child",
                    config_file="/etc/vector/child.yaml",
                    parent="root",
                ),
            ],
        )
        assert len(cfg.sources) == 2
        assert cfg.sources[0].parent == ""
        assert cfg.sources[1].parent == "root"

    def test_source_with_files(self):
        src = VectorSourceConfig(
            name="enriched",
            config_file="/etc/vector/enriched.yaml",
            files=[
                SourceFileConfig(path="/data/geo.mmdb", type="mmdb"),
                SourceFileConfig(path="/data/ioc.csv", type="csv"),
            ],
            env={"GEO_DB_PATH": "/data/geo.mmdb"},
        )
        assert len(src.files) == 2
        assert src.env["GEO_DB_PATH"] == "/data/geo.mmdb"

    def test_json_roundtrip(self):
        cfg = TransformVectorConfig(
            sources=[
                VectorSourceConfig(
                    name="src1",
                    config_file="/etc/vector/src1.yaml",
                    env={"KEY": "val"},
                    files=[SourceFileConfig(path="/data/f.csv")],
                ),
            ],
            extra_yaml_files=["/etc/vector/global.yaml"],
            extra_env={"GLOBAL": "true"},
        )
        data = cfg.model_dump(mode="json")
        restored = TransformVectorConfig.model_validate(data)
        assert restored.sources[0].name == "src1"
        assert restored.extra_yaml_files == ["/etc/vector/global.yaml"]
        assert restored.extra_env["GLOBAL"] == "true"

    def test_forbids_extra_fields(self):
        with pytest.raises(Exception):
            TransformVectorConfig(unknown_field="bad")


class TestTransformWasmConfig:
    """Test transform-wasm configuration model."""

    def test_inherits_base_service_config(self):
        assert issubclass(TransformWasmConfig, BaseServiceConfig)

    def test_wasm_source_inherits_base_source(self):
        assert issubclass(WasmSourceConfig, BaseSourceConfig)

    def test_empty_config(self):
        cfg = TransformWasmConfig()
        assert cfg.sources == []
        assert cfg.extra_env == {}
        assert cfg.ipc.enabled is False

    def test_with_sources(self):
        cfg = TransformWasmConfig(
            sources=[
                WasmSourceConfig(
                    name="transform1",
                    wasm_module="/opt/wasm/transform.wasm",
                    input_topic="raw",
                    output_topic="enriched",
                ),
            ],
        )
        assert len(cfg.sources) == 1
        assert cfg.sources[0].wasm_module == "/opt/wasm/transform.wasm"

    def test_wasm_source_with_files(self):
        src = WasmSourceConfig(
            name="wasm-src",
            wasm_module="/opt/wasm/transform.wasm",
            files=[SourceFileConfig(path="/data/lookup.csv", type="csv")],
            env={"WASM_LOG": "debug"},
        )
        assert len(src.files) == 1
        assert src.env["WASM_LOG"] == "debug"

    def test_json_roundtrip(self):
        cfg = TransformWasmConfig(
            sources=[
                WasmSourceConfig(
                    name="w1",
                    wasm_module="/opt/wasm/w1.wasm",
                    input_topic="raw",
                    output_topic="processed",
                ),
            ],
        )
        data = cfg.model_dump(mode="json")
        restored = TransformWasmConfig.model_validate(data)
        assert restored.sources[0].wasm_module == "/opt/wasm/w1.wasm"


class TestFetcherConfig:
    """Test fetcher configuration model."""

    def test_inherits_base_service_config(self):
        assert issubclass(FetcherConfig, BaseServiceConfig)

    def test_fetcher_source_inherits_base_source(self):
        assert issubclass(FetcherSourceConfig, BaseSourceConfig)

    def test_empty_config(self):
        cfg = FetcherConfig()
        assert cfg.sources == []
        assert cfg.extra_env == {}
        assert cfg.routing.default_topic == "unmatched"

    def test_with_sources(self):
        cfg = FetcherConfig(
            sources=[
                FetcherSourceConfig(
                    name="ms-graph",
                    source_type="microsoft_graph",
                    base_url="https://graph.microsoft.com/v1.0",
                    auth=FetcherAuthConfig(
                        type="oauth2",
                        token_url="https://login.microsoftonline.com/tenant/oauth2/v2.0/token",
                    ),
                    poll_interval_secs=300,
                ),
            ],
        )
        assert len(cfg.sources) == 1
        assert cfg.sources[0].source_type == "microsoft_graph"
        assert cfg.sources[0].auth.type == "oauth2"

    def test_source_with_files(self):
        src = FetcherSourceConfig(
            name="enriched-src",
            source_type="custom_api",
            files=[SourceFileConfig(path="/data/mapping.json", type="json")],
            env={"API_VERSION": "v2"},
        )
        assert len(src.files) == 1
        assert src.env["API_VERSION"] == "v2"

    def test_auth_config_types(self):
        # OAuth2
        auth = FetcherAuthConfig(type="oauth2", token_url="https://example.com/token")
        assert auth.type == "oauth2"

        # API key
        auth = FetcherAuthConfig(type="api_key", api_key="secret123")
        assert auth.api_key.get_secret_value() == "secret123"

        # Bearer
        auth = FetcherAuthConfig(type="bearer", bearer_token="tok123")
        assert auth.bearer_token.get_secret_value() == "tok123"

    def test_json_roundtrip(self):
        cfg = FetcherConfig(
            sources=[
                FetcherSourceConfig(
                    name="okta",
                    source_type="okta",
                    base_url="https://dev.okta.com/api/v1",
                    auth=FetcherAuthConfig(type="api_key"),
                ),
            ],
            routing={"default_topic": "fetcher_land"},
        )
        data = cfg.model_dump(mode="json")
        restored = FetcherConfig.model_validate(data)
        assert restored.sources[0].source_type == "okta"
        assert restored.routing.default_topic == "fetcher_land"

    def test_poll_interval_minimum(self):
        with pytest.raises(Exception):
            FetcherSourceConfig(
                name="bad",
                source_type="test",
                poll_interval_secs=5,
            )
