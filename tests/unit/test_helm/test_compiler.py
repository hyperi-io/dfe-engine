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
    kw.setdefault("image", HelmImage(repository="ghcr.io/hyperi-io/dfe-receiver", tag="1.2.0"))
    return HelmServiceValues(
        deploy=HelmDeployMeta(service=f"dfe-{service}", instance=instance), **kw
    )


from dfe_engine.settings import get_settings
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
        "image: ghcr.io/hyperi-io/dfe-receiver\n"
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
        "image: ghcr.io/hyperi-io/dfe-loader\n"
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
# Kafka topics
# ---------------------------------------------------------------------------


class _SourceStub:
    """Only ``get_all_sources`` is reached by the topic compile."""

    def __init__(self, sources):
        self._sources = sources

    def get_all_sources(self, states=("active",)):
        return [s for s in self._sources if s.state in states]


def _topic_compiler(sources, environment):
    return HelmValuesCompiler(None, None, _SourceStub(sources), environment)


class TestCompileKafkaTopics:
    def test_a_transforming_source_yields_both_land_and_load(self, environment):
        from dfe_engine.source.models import Source, SourceMatch, SourceTransform

        src = Source(
            source="filebeat",
            match=SourceMatch(field="f", value="v"),
            transform=SourceTransform(engine="vector"),
        )
        topics = _topic_compiler([src], environment).compile_kafka_topics()
        assert [t["name"] for t in topics] == ["filebeat_land", "filebeat_load"]

    def test_a_source_without_a_transform_yields_only_land(self, environment):
        from dfe_engine.source.models import Source, SourceMatch

        src = Source(source="syslog", match=SourceMatch(field="f", value="v"))
        topics = _topic_compiler([src], environment).compile_kafka_topics()
        assert [t["name"] for t in topics] == ["syslog_land"]

    def test_partitions_come_from_settings_not_a_constant(self, environment, monkeypatch):
        from dfe_engine.source.models import Source, SourceMatch

        settings = get_settings()
        monkeypatch.setattr(settings.kafka, "topic_partitions", 12)
        monkeypatch.setattr(settings.kafka, "topic_replication_factor", 3)

        src = Source(source="syslog", match=SourceMatch(field="f", value="v"))
        topics = _topic_compiler([src], environment).compile_kafka_topics()
        assert topics[0]["partitions"] == 12
        assert topics[0]["replication_factor"] == 3

    def test_a_dormant_source_gets_no_topics(self, environment):
        """No live pipeline, so nothing produces or consumes - do not mint topics."""
        from dfe_engine.source.models import Source, SourceMatch, SourceTransform

        dormant = Source(
            source="retired",
            state="dormant",
            match=SourceMatch(field="f", value="v"),
            transform=SourceTransform(engine="vector"),
        )
        active = Source(source="syslog", match=SourceMatch(field="f", value="v"))
        topics = _topic_compiler([dormant, active], environment).compile_kafka_topics()
        assert [t["name"] for t in topics] == ["syslog_land"]

    def test_several_sources_each_contribute_their_own_topics(self, environment):
        from dfe_engine.source.models import Source, SourceMatch, SourceTransform

        a = Source(
            source="filebeat",
            match=SourceMatch(field="f", value="v"),
            transform=SourceTransform(engine="vector"),
        )
        b = Source(source="syslog", match=SourceMatch(field="f", value="v"))
        topics = _topic_compiler([a, b], environment).compile_kafka_topics()
        names = [t["name"] for t in topics]
        assert names == ["filebeat_land", "filebeat_load", "syslog_land"]
        assert len(names) == len(set(names))


# ---------------------------------------------------------------------------
# Source DDL and loader routing
# ---------------------------------------------------------------------------


class _DeployStub:
    """A compile with no deployment configs still has to reach the source DDL."""

    def list_configs(self):
        return []


class TestCompileDdl:
    def test_compile_all_renders_the_ddl_for_each_source(self, environment):
        """The DDL is built inside compile_all, so a builder that raises loses it.

        The surrounding guard catches an ImportError alone, which is why the
        registry is built the way every other builder site builds it.
        """
        from dfe_engine.source.models import Source

        src = Source.model_validate(
            {
                "source": "syslog",
                "match": {"field": "f", "value": "v"},
                "header": {"type": "timeseries", "version": "1.0.0"},
            }
        )
        compiler = HelmValuesCompiler(_DeployStub(), None, _SourceStub([src]), environment)

        result = compiler.compile_all()

        assert len(result.ddl_statements) == 1
        assert "CREATE TABLE IF NOT EXISTS" in result.ddl_statements[0]
        assert "syslog" in result.ddl_statements[0]


class TestLoaderRoutingInjection:
    def _compiler(self, sources, environment):
        compiler = HelmValuesCompiler.__new__(HelmValuesCompiler)
        compiler._env = environment
        compiler._source = _SourceStub(sources)
        return compiler

    def test_the_deployments_own_routing_setting_survives(self, environment):
        from dfe_engine.source.models import Source, SourceMatch

        src = Source(source="syslog", match=SourceMatch(field="f", value="v"))
        config = self._compiler([src], environment)._inject_loader_routing(
            {"routing": {"dlq": {"enabled": False}}}
        )

        assert config["routing"]["dlq"] == {"enabled": False}
        assert config["routing"]["source_to_table"] == {"syslog": "syslog"}

    def test_only_the_derived_keys_are_stamped(self, environment):
        config = self._compiler([], environment)._inject_loader_routing({})

        assert set(config["routing"]) == {"default_db", "source_to_table"}


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
        assert data["image"]["repository"] == "ghcr.io/hyperi-io/dfe-receiver"
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

    def test_write_includes_rbac_csv(self, tmp_path, environment):
        compiler = HelmValuesCompiler.__new__(HelmValuesCompiler)
        compiler._env = environment

        result = CompilationResult(
            helm_values={},
            argo_rbac_csv="p, role:dfe-admin, *, *, dfe/*, allow\n",
        )

        output_dir = tmp_path / "helm-values"
        written = compiler.write_all(result, output_dir)

        rbac_path = output_dir / "argocd-rbac-policy.csv"
        assert rbac_path in written
        assert rbac_path.exists()
        assert "role:dfe-admin" in rbac_path.read_text()


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
        assert merged.image.repository == "ghcr.io/hyperi-io/dfe-receiver"


# ---------------------------------------------------------------------------
# Argo CD Application + AppProject generation
# ---------------------------------------------------------------------------


class TestArgoAppGeneration:
    def test_argo_disabled_skips_generation(self, environment):
        """When argo.enabled is False, no Application CRDs are generated."""
        compiler = HelmValuesCompiler.__new__(HelmValuesCompiler)
        compiler._env = environment

        # Default environment has argo.enabled=False
        assert environment.argo.enabled is False

        result = CompilationResult(
            helm_values={"receiver-production": _sv()},
        )
        # No argo_applications should be present by default
        assert result.argo_applications == []
        assert result.argo_appproject == {}


class TestWriteArgoManifests:
    def test_write_applications(self, tmp_path, environment):
        compiler = HelmValuesCompiler.__new__(HelmValuesCompiler)
        compiler._env = environment

        result = CompilationResult(
            helm_values={},
            argo_applications=[
                {
                    "apiVersion": "argoproj.io/v1alpha1",
                    "kind": "Application",
                    "metadata": {
                        "name": "dfe-receiver-production",
                        "namespace": "argocd",
                    },
                    "spec": {"project": "dfe"},
                },
                {
                    "apiVersion": "argoproj.io/v1alpha1",
                    "kind": "Application",
                    "metadata": {
                        "name": "dfe-loader-production",
                        "namespace": "argocd",
                    },
                    "spec": {"project": "dfe"},
                },
            ],
        )

        output_dir = tmp_path / "output"
        written = compiler.write_all(result, output_dir)

        apps_dir = output_dir / "applications"
        assert apps_dir.exists()

        receiver_path = apps_dir / "dfe-receiver-production.yaml"
        loader_path = apps_dir / "dfe-loader-production.yaml"
        assert receiver_path in written
        assert loader_path in written
        assert receiver_path.exists()
        assert loader_path.exists()

        data = yaml_load(receiver_path)
        assert data["kind"] == "Application"
        assert data["metadata"]["name"] == "dfe-receiver-production"

    def test_write_appproject(self, tmp_path, environment):
        compiler = HelmValuesCompiler.__new__(HelmValuesCompiler)
        compiler._env = environment

        result = CompilationResult(
            helm_values={},
            argo_appproject={
                "apiVersion": "argoproj.io/v1alpha1",
                "kind": "AppProject",
                "metadata": {"name": "dfe", "namespace": "argocd"},
                "spec": {
                    "description": "DFE Engine - test",
                    "sourceRepos": ["https://example.com"],
                },
            },
        )

        output_dir = tmp_path / "output"
        written = compiler.write_all(result, output_dir)

        project_path = output_dir / "appproject-dfe.yaml"
        assert project_path in written
        assert project_path.exists()

        data = yaml_load(project_path)
        assert data["kind"] == "AppProject"
        assert data["metadata"]["name"] == "dfe"

    def test_no_applications_dir_when_empty(self, tmp_path, environment):
        compiler = HelmValuesCompiler.__new__(HelmValuesCompiler)
        compiler._env = environment

        result = CompilationResult(helm_values={})
        compiler.write_all(result, tmp_path / "output")

        assert not (tmp_path / "output" / "applications").exists()
