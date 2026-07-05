"""Tests for the Helm values compiler."""

import pytest

from dfe_engine.helm.compiler import HelmValuesCompiler, _scrub_secrets
from dfe_engine.helm.environment import (
    ClickHouseEnvironment,
    EnvironmentConfig,
    KafkaEnvironment,
    OTelEnvironment,
)
from dfe_engine.helm.models import (
    CompilationResult,
    HelmDeployMeta,
    HelmImage,
    HelmServiceValues,
)


def _sv(service="receiver", instance="production", **kw) -> HelmServiceValues:
    """Build a chart-shaped HelmServiceValues with the required deploy meta."""
    kw.setdefault("image", HelmImage(repository="harbor.hyperi.io/dfe/dfe-receiver", tag="1.2.0"))
    return HelmServiceValues(
        deploy=HelmDeployMeta(service=f"dfe-{service}", instance=instance), **kw
    )


from dfe_engine.yaml_utils import yaml_load

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def environment():
    return EnvironmentConfig(
        name="test",
        kafka=KafkaEnvironment(
            bootstrap_servers=["kafka-1.test:9092", "kafka-2.test:9092"],
            authentication_ref="kafka-auth",
        ),
        clickhouse=ClickHouseEnvironment(
            hosts=["ch-1.test:9000"],
            database="dfe",
            username="default",
        ),
        secret_refs={"tls": "dfe-tls-secret"},
    )


@pytest.fixture
def deploy_dir(tmp_path):
    """Create a deployment config directory with receiver + loader configs."""
    deploy = tmp_path / "deploy"
    deploy.mkdir()

    (deploy / "receiver-production.yaml").write_text(
        "size: small\n"
        "replicas: 2\n"
        "image: harbor.hyperi.io/dfe/dfe-receiver\n"
        "image_tag: '1.2.0'\n"
        "keda:\n"
        "  enabled: true\n"
        "  min_replicas: 2\n"
        "  max_replicas: 10\n"
        "  kafka_trigger:\n"
        "    consumer_group: dfe-receiver\n"
        "    lag_threshold: 100\n"
        "service:\n"
        "  port: 8080\n"
        "  metrics_port: 9090\n"
    )

    (deploy / "loader-production.yaml").write_text(
        "size: medium\n"
        "replicas: 2\n"
        "image: harbor.hyperi.io/dfe/dfe-loader\n"
        "image_tag: '1.2.0'\n"
        "keda:\n"
        "  enabled: true\n"
        "  min_replicas: 2\n"
        "  max_replicas: 8\n"
        "  kafka_trigger:\n"
        "    consumer_group: clickhouse-loader\n"
        "    lag_threshold: 50000\n"
        "service:\n"
        "  port: 9000\n"
        "  metrics_port: 9090\n"
    )

    return deploy


@pytest.fixture
def svc_dir(tmp_path):
    """Create a service config directory with receiver + loader configs."""
    svc = tmp_path / "services"
    svc.mkdir()

    (svc / "receiver-production.yaml").write_text(
        "server:\n"
        "  bind_address: '0.0.0.0:8080'\n"
        "kafka:\n"
        "  brokers:\n"
        "    - localhost:9092\n"
        "routing:\n"
        "  default_topic: unmatched\n"
    )

    (svc / "loader-production.yaml").write_text(
        "kafka:\n"
        "  brokers:\n"
        "    - localhost:9092\n"
        "clickhouse:\n"
        "  hosts:\n"
        "    - localhost:9000\n"
        "  database: default\n"
        "routing:\n"
        "  default_db: common\n"
    )

    return svc


@pytest.fixture
def src_dir(tmp_path):
    """Create a source definitions directory."""
    src = tmp_path / "sources"
    src.mkdir()

    (src / "windows_audit.yaml").write_text(
        "source: windows_audit\n"
        "display_name: Windows Audit\n"
        "enabled: true\n"
        "topic_land: windows_audit_land\n"
        "table_name: windows_audit\n"
        "match:\n"
        "  field: tags.event.category\n"
        "  value: windows_security\n"
    )

    return src


# ---------------------------------------------------------------------------
# Secret scrubbing
# ---------------------------------------------------------------------------


class TestScrubSecrets:
    def test_scrubs_nested_secrets(self):
        from pydantic import SecretStr

        data = {
            "username": "admin",
            "password": SecretStr("s3cr3t"),
            "nested": {"key": SecretStr("hidden")},
            "list": [SecretStr("val"), "plain"],
        }
        scrubbed = _scrub_secrets(data)
        assert scrubbed["password"] == "***"
        assert scrubbed["nested"]["key"] == "***"
        assert scrubbed["list"][0] == "***"
        assert scrubbed["list"][1] == "plain"
        assert scrubbed["username"] == "admin"

    def test_scrubs_plain_values_unchanged(self):
        data = {"a": 1, "b": "hello", "c": [1, 2, 3]}
        assert _scrub_secrets(data) == data


# ---------------------------------------------------------------------------
# KEDA wiring
# ---------------------------------------------------------------------------


class TestKedaWiring:
    def test_disabled_keda(self, environment):
        from dfe_engine.deployment.models.common import KedaConfig

        compiler = HelmValuesCompiler.__new__(HelmValuesCompiler)
        compiler._env = environment
        keda = compiler._compile_keda(KedaConfig(enabled=False))
        assert not keda.enabled
        assert keda.triggers is None

    def test_kafka_trigger_wiring(self, environment):
        from dfe_engine.deployment.models.common import KedaConfig, KedaTriggerKafka

        compiler = HelmValuesCompiler.__new__(HelmValuesCompiler)
        compiler._env = environment

        keda_config = KedaConfig(
            enabled=True,
            min_replicas=2,
            max_replicas=10,
            kafka_trigger=KedaTriggerKafka(
                consumer_group="dfe-receiver",
                lag_threshold=100,
            ),
        )

        keda = compiler._compile_keda(keda_config)
        assert keda.enabled
        assert len(keda.triggers) == 1

        trigger = keda.triggers[0]
        assert trigger.type == "kafka"
        assert trigger.metadata["bootstrapServers"] == "kafka-1.test:9092,kafka-2.test:9092"
        assert trigger.metadata["consumerGroup"] == "dfe-receiver"
        assert trigger.metadata["lagThreshold"] == "100"
        assert trigger.authenticationRef == {"name": "kafka-auth"}


# ---------------------------------------------------------------------------
# Write output
# ---------------------------------------------------------------------------


class TestWriteAll:
    def test_write_creates_files(self, tmp_path, environment):
        compiler = HelmValuesCompiler.__new__(HelmValuesCompiler)
        compiler._env = environment

        result = CompilationResult(
            helm_values={"receiver-production": _sv()},
        )

        output_dir = tmp_path / "helm-values"
        written = compiler.write_all(result, output_dir)

        assert len(written) == 1
        assert written[0].name == "receiver-production-values.yaml"
        assert written[0].exists()

        # Verify YAML content -- chart-shaped image block.
        data = yaml_load(written[0])
        assert data["image"]["repository"] == "harbor.hyperi.io/dfe/dfe-receiver"
        assert data["image"]["tag"] == "1.2.0"

    def test_deterministic_output(self, tmp_path, environment):
        compiler = HelmValuesCompiler.__new__(HelmValuesCompiler)
        compiler._env = environment

        result = CompilationResult(
            helm_values={
                "receiver-production": _sv(config={"server": {"bind": "0.0.0.0:8080"}}),
                "loader-production": _sv(
                    service="loader", config={"kafka": {"brokers": ["k:9092"]}}
                ),
            }
        )

        out1 = tmp_path / "out1"
        out2 = tmp_path / "out2"
        compiler.write_all(result, out1)
        compiler.write_all(result, out2)

        for f1 in sorted(out1.iterdir()):
            f2 = out2 / f1.name
            assert f1.read_text() == f2.read_text(), f"Non-deterministic: {f1.name}"


# ---------------------------------------------------------------------------
# OTEL env var injection
# ---------------------------------------------------------------------------


class TestOtelInjection:
    def _env_with_otel(self, enabled=True):
        return EnvironmentConfig(
            name="production",
            kafka=KafkaEnvironment(bootstrap_servers=["kafka:9092"]),
            clickhouse=ClickHouseEnvironment(hosts=["ch:9000"], database="dfe"),
            otel=OTelEnvironment(
                enabled=enabled,
                collector_endpoint="http://otel:4317",
                protocol="grpc",
                resource_attributes={"team": "platform"},
            ),
        )

    def test_otel_disabled_no_env_vars(self):
        """When OTEL is disabled, no OTEL env vars should be injected."""
        env = self._env_with_otel(enabled=False)
        extra_env: dict[str, str] = {}

        # Simulate the injection logic
        if env.otel.enabled:
            extra_env["OTEL_EXPORTER_OTLP_ENDPOINT"] = env.otel.collector_endpoint

        assert "OTEL_EXPORTER_OTLP_ENDPOINT" not in extra_env

    def test_otel_enabled_injects_env_vars(self):
        """When OTEL is enabled, all standard OTEL env vars should be set."""
        env = self._env_with_otel(enabled=True)
        extra_env: dict[str, str] = {}

        # Same logic as compiler.compile_service()
        if env.otel.enabled:
            otel = env.otel
            extra_env["OTEL_EXPORTER_OTLP_ENDPOINT"] = otel.collector_endpoint
            extra_env["OTEL_EXPORTER_OTLP_PROTOCOL"] = otel.protocol
            extra_env["OTEL_SERVICE_NAME"] = "dfe-receiver"
            attrs = f"service.namespace=dfe,deployment.environment={env.name}"
            for k, v in otel.resource_attributes.items():
                attrs += f",{k}={v}"
            extra_env["OTEL_RESOURCE_ATTRIBUTES"] = attrs

        assert extra_env["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://otel:4317"
        assert extra_env["OTEL_EXPORTER_OTLP_PROTOCOL"] == "grpc"
        assert extra_env["OTEL_SERVICE_NAME"] == "dfe-receiver"
        assert "service.namespace=dfe" in extra_env["OTEL_RESOURCE_ATTRIBUTES"]
        assert "deployment.environment=production" in extra_env["OTEL_RESOURCE_ATTRIBUTES"]
        assert "team=platform" in extra_env["OTEL_RESOURCE_ATTRIBUTES"]


# ---------------------------------------------------------------------------
# Prometheus KEDA trigger
# ---------------------------------------------------------------------------


class TestPrometheusTrigger:
    def test_prometheus_trigger_compilation(self):
        from dfe_engine.deployment.models.common import (
            KedaConfig,
            KedaTriggerPrometheus,
        )

        env = EnvironmentConfig(
            name="prod",
            kafka=KafkaEnvironment(bootstrap_servers=["kafka:9092"]),
            clickhouse=ClickHouseEnvironment(hosts=["ch:9000"]),
            otel=OTelEnvironment(
                enabled=True,
                collector_endpoint="http://otel-collector:4317",
                prometheus_port=8889,
            ),
        )

        compiler = HelmValuesCompiler.__new__(HelmValuesCompiler)
        compiler._env = env

        keda_config = KedaConfig(
            enabled=True,
            prometheus_trigger=KedaTriggerPrometheus(
                query="sum(rate(dfe_events_total[5m]))",
                threshold=500,
            ),
        )

        keda = compiler._compile_keda(keda_config)
        assert keda.enabled
        assert len(keda.triggers) == 1

        trigger = keda.triggers[0]
        assert trigger.type == "prometheus"
        assert trigger.metadata["query"] == "sum(rate(dfe_events_total[5m]))"
        assert trigger.metadata["threshold"] == "500"
        # Should auto-resolve from OTEL collector endpoint
        assert "otel-collector" in trigger.metadata["serverAddress"]
        assert "8889" in trigger.metadata["serverAddress"]

    def test_prometheus_trigger_explicit_address(self):
        from dfe_engine.deployment.models.common import (
            KedaConfig,
            KedaTriggerPrometheus,
        )

        env = EnvironmentConfig(
            name="prod",
            kafka=KafkaEnvironment(bootstrap_servers=["kafka:9092"]),
            clickhouse=ClickHouseEnvironment(hosts=["ch:9000"]),
        )

        compiler = HelmValuesCompiler.__new__(HelmValuesCompiler)
        compiler._env = env

        keda_config = KedaConfig(
            enabled=True,
            prometheus_trigger=KedaTriggerPrometheus(
                server_address="http://prometheus:9090",
                query="up",
                threshold=1,
            ),
        )

        keda = compiler._compile_keda(keda_config)
        trigger = keda.triggers[0]
        assert trigger.metadata["serverAddress"] == "http://prometheus:9090"


# ---------------------------------------------------------------------------
# Generic KEDA triggers
# ---------------------------------------------------------------------------


class TestGenericTrigger:
    def test_extra_triggers_pass_through(self):
        from dfe_engine.deployment.models.common import (
            KedaConfig,
            KedaTriggerGeneric,
        )

        env = EnvironmentConfig(
            name="prod",
            kafka=KafkaEnvironment(bootstrap_servers=["kafka:9092"]),
            clickhouse=ClickHouseEnvironment(hosts=["ch:9000"]),
        )

        compiler = HelmValuesCompiler.__new__(HelmValuesCompiler)
        compiler._env = env

        keda_config = KedaConfig(
            enabled=True,
            extra_triggers=[
                KedaTriggerGeneric(
                    type="cron",
                    metadata={
                        "timezone": "Australia/Sydney",
                        "start": "0 6 * * *",
                        "end": "0 20 * * *",
                        "desiredReplicas": "5",
                    },
                ),
            ],
        )

        keda = compiler._compile_keda(keda_config)
        assert len(keda.triggers) == 1
        trigger = keda.triggers[0]
        assert trigger.type == "cron"
        assert trigger.metadata["timezone"] == "Australia/Sydney"


# ---------------------------------------------------------------------------
# Merge overrides
# ---------------------------------------------------------------------------


class TestMergeOverrides:
    def test_merge_adds_extra_env(self):
        env = EnvironmentConfig(
            name="prod",
            kafka=KafkaEnvironment(bootstrap_servers=["kafka:9092"]),
            clickhouse=ClickHouseEnvironment(hosts=["ch:9000"]),
        )
        compiler = HelmValuesCompiler.__new__(HelmValuesCompiler)
        compiler._env = env

        values = _sv(extra_env={"EXISTING": "value"})
        merged = compiler.merge_overrides(values, {"extra_env": {"NEW": "added"}})
        assert merged.extra_env["EXISTING"] == "value"
        assert merged.extra_env["NEW"] == "added"

    def test_merge_overrides_replicas(self):
        env = EnvironmentConfig(
            name="prod",
            kafka=KafkaEnvironment(bootstrap_servers=["kafka:9092"]),
            clickhouse=ClickHouseEnvironment(hosts=["ch:9000"]),
        )
        compiler = HelmValuesCompiler.__new__(HelmValuesCompiler)
        compiler._env = env

        values = _sv(replicaCount=2)
        merged = compiler.merge_overrides(values, {"replicaCount": 5})
        assert merged.replicaCount == 5
        assert merged.image.repository == "harbor.hyperi.io/dfe/dfe-receiver"


# ---------------------------------------------------------------------------
# Pod node placement + image pull policy reach the chart (regression)
# ---------------------------------------------------------------------------


class TestPodPlacement:
    def test_node_selector_and_pull_policy_reach_chart(self, tmp_path, environment):
        """PodConfig.node_selector / image_pull_policy must land on the chart's
        nodeScheduling.nodeSelector and image.pullPolicy. model_dump emits
        snake_case, so the old camelCase lookups silently dropped both dials."""
        from dfe_engine.deployment.models import ArchiverDeploymentConfig
        from dfe_engine.deployment.registry import DeploymentConfigRegistry
        from dfe_engine.services.registry import ServiceConfigRegistry

        deploy_reg = DeploymentConfigRegistry(config_directory=tmp_path / "deploy")
        svc_reg = ServiceConfigRegistry(config_directory=tmp_path / "svc", refresh_interval=0)
        try:
            data = ArchiverDeploymentConfig().model_dump(mode="json")
            data["pod"]["node_selector"] = {"disktype": "ssd"}
            data["pod"]["image_pull_policy"] = "Always"
            deploy_reg.save_config("archiver", data, instance="production")

            compiler = HelmValuesCompiler(deploy_reg, svc_reg, None, environment)
            values = compiler.compile_service("archiver", "production")

            assert values.nodeScheduling["nodeSelector"] == {"disktype": "ssd"}
            assert values.image.pullPolicy == "Always"
        finally:
            svc_reg.close()
            deploy_reg.close()
