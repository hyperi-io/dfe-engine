"""Tests for Helm environment config."""

import pytest

from dfe_engine.helm.environment import (
    ArgoEnvironment,
    ArgoSyncPolicy,
    ClickHouseEnvironment,
    EnvironmentConfig,
    KafkaEnvironment,
    OTelEnvironment,
)


class TestKafkaEnvironment:
    def test_basic(self):
        k = KafkaEnvironment(bootstrap_servers=["kafka-1:9092", "kafka-2:9092"])
        assert len(k.bootstrap_servers) == 2
        assert k.authentication_ref == ""

    def test_with_auth_ref(self):
        k = KafkaEnvironment(
            bootstrap_servers=["kafka:9092"],
            authentication_ref="kafka-auth",
        )
        assert k.authentication_ref == "kafka-auth"


class TestClickHouseEnvironment:
    def test_defaults(self):
        ch = ClickHouseEnvironment(hosts=["ch-1:9000"])
        assert ch.database == "default"
        assert ch.username == "default"
        assert ch.secure is True

    def test_password_is_secret(self):
        ch = ClickHouseEnvironment(hosts=["ch:9000"], password="secret123")
        assert ch.password.get_secret_value() == "secret123"


class TestEnvironmentConfig:
    def test_minimal(self):
        env = EnvironmentConfig(
            name="production",
            kafka=KafkaEnvironment(bootstrap_servers=["kafka:9092"]),
            clickhouse=ClickHouseEnvironment(hosts=["ch:9000"]),
        )
        assert env.name == "production"
        assert env.namespace == "dfe"
        assert env.image_registry == "ghcr.io/hyperi-io"

    def test_image_tag_override(self):
        env = EnvironmentConfig(
            name="staging",
            kafka=KafkaEnvironment(bootstrap_servers=["kafka:9092"]),
            clickhouse=ClickHouseEnvironment(hosts=["ch:9000"]),
            image_tag_override="1.5.0-rc1",
        )
        assert env.image_tag_override == "1.5.0-rc1"

    def test_secret_refs(self):
        env = EnvironmentConfig(
            name="prod",
            kafka=KafkaEnvironment(bootstrap_servers=["kafka:9092"]),
            clickhouse=ClickHouseEnvironment(hosts=["ch:9000"]),
            secret_refs={"config-secrets": "dfe-receiver-secrets"},
        )
        assert env.secret_refs["config-secrets"] == "dfe-receiver-secrets"

    def test_to_dict_scrubs_password(self):
        env = EnvironmentConfig(
            name="prod",
            kafka=KafkaEnvironment(bootstrap_servers=["kafka:9092"]),
            clickhouse=ClickHouseEnvironment(hosts=["ch:9000"], password="s3cr3t"),
        )
        d = env.to_dict()
        assert d["clickhouse"]["password"] == "***"

    def test_from_yaml(self, tmp_path):
        yaml_file = tmp_path / "env.yaml"
        yaml_file.write_text(
            "name: test\n"
            "kafka:\n"
            "  bootstrap_servers:\n"
            "    - kafka:9092\n"
            "clickhouse:\n"
            "  hosts:\n"
            "    - ch:9000\n"
        )
        env = EnvironmentConfig.from_yaml(yaml_file)
        assert env.name == "test"
        assert env.kafka.bootstrap_servers == ["kafka:9092"]

    def test_from_yaml_empty_raises(self, tmp_path):
        yaml_file = tmp_path / "empty.yaml"
        yaml_file.write_text("")
        with pytest.raises(ValueError, match="Empty or invalid"):
            EnvironmentConfig.from_yaml(yaml_file)

    def test_otel_default_disabled(self):
        env = EnvironmentConfig(
            name="prod",
            kafka=KafkaEnvironment(bootstrap_servers=["kafka:9092"]),
            clickhouse=ClickHouseEnvironment(hosts=["ch:9000"]),
        )
        assert env.otel.enabled is False
        assert env.otel.collector_endpoint == "http://otel-collector:4317"

    def test_otel_enabled(self):
        env = EnvironmentConfig(
            name="prod",
            kafka=KafkaEnvironment(bootstrap_servers=["kafka:9092"]),
            clickhouse=ClickHouseEnvironment(hosts=["ch:9000"]),
            otel=OTelEnvironment(
                enabled=True,
                collector_endpoint="http://otel:4317",
                resource_attributes={"team": "platform"},
            ),
        )
        assert env.otel.enabled
        assert env.otel.resource_attributes["team"] == "platform"


class TestOTelEnvironment:
    def test_defaults(self):
        otel = OTelEnvironment()
        assert otel.enabled is False
        assert otel.collector_endpoint == "http://otel-collector:4317"
        assert otel.protocol == "grpc"
        assert otel.prometheus_port == 8889
        assert otel.resource_attributes == {}

    def test_custom(self):
        otel = OTelEnvironment(
            enabled=True,
            collector_endpoint="http://custom:4317",
            protocol="http/protobuf",
            prometheus_port=9090,
            resource_attributes={"env": "prod"},
        )
        assert otel.enabled
        assert otel.protocol == "http/protobuf"
        assert otel.prometheus_port == 9090


class TestArgoSyncPolicy:
    def test_defaults(self):
        sp = ArgoSyncPolicy()
        assert sp.auto_sync is True
        assert sp.prune is True
        assert sp.self_heal is True
        assert sp.retry_limit == 5
        assert "CreateNamespace=true" in sp.sync_options

    def test_to_argo_dict_defaults(self):
        d = ArgoSyncPolicy().to_argo_dict()
        assert d["automated"]["prune"] is True
        assert d["automated"]["selfHeal"] is True
        assert "CreateNamespace=true" in d["syncOptions"]
        assert d["retry"]["limit"] == 5
        assert d["retry"]["backoff"]["factor"] == 2

    def test_to_argo_dict_no_auto_sync(self):
        sp = ArgoSyncPolicy(auto_sync=False)
        d = sp.to_argo_dict()
        assert "automated" not in d

    def test_to_argo_dict_no_retry(self):
        sp = ArgoSyncPolicy(retry_limit=0)
        d = sp.to_argo_dict()
        assert "retry" not in d

    def test_custom_sync_options(self):
        sp = ArgoSyncPolicy(sync_options=["ServerSideApply=true", "PruneLast=true"])
        d = sp.to_argo_dict()
        assert "ServerSideApply=true" in d["syncOptions"]
        assert "PruneLast=true" in d["syncOptions"]


class TestArgoEnvironment:
    def test_defaults(self):
        argo = ArgoEnvironment()
        assert argo.enabled is False
        assert argo.project == "dfe"
        assert argo.chart_repo_url == ""
        assert argo.chart_version == ""
        assert argo.destination_server == "https://kubernetes.default.svc"
        assert argo.values_path_prefix == "values"

    def test_custom(self):
        argo = ArgoEnvironment(
            enabled=True,
            project="custom",
            chart_repo_url="https://charts.example.com",
            chart_version="2.0.0",
            chart_overrides={"receiver": "my-receiver"},
        )
        assert argo.enabled
        assert argo.project == "custom"
        assert argo.chart_overrides["receiver"] == "my-receiver"

    def test_env_config_argo_default(self):
        env = EnvironmentConfig(
            name="prod",
            kafka=KafkaEnvironment(bootstrap_servers=["kafka:9092"]),
            clickhouse=ClickHouseEnvironment(hosts=["ch:9000"]),
        )
        assert env.argo.enabled is False
        assert env.argo.project == "dfe"

    def test_env_config_argo_enabled(self):
        env = EnvironmentConfig(
            name="prod",
            kafka=KafkaEnvironment(bootstrap_servers=["kafka:9092"]),
            clickhouse=ClickHouseEnvironment(hosts=["ch:9000"]),
            argo=ArgoEnvironment(
                enabled=True,
                chart_repo_url="https://charts.example.com/dfe",
                source_repos=["https://charts.example.com/dfe"],
            ),
        )
        assert env.argo.enabled
        assert env.argo.chart_repo_url == "https://charts.example.com/dfe"
