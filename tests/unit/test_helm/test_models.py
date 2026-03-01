"""Tests for Helm values models."""

from dfe_engine.helm.models import (
    CompilationResult,
    HelmKedaConfig,
    HelmKedaTrigger,
    HelmServiceValues,
)


class TestHelmKedaTrigger:
    def test_defaults(self):
        t = HelmKedaTrigger()
        assert t.type == "kafka"
        assert t.metadata == {}
        assert t.authentication_ref == ""

    def test_kafka_trigger(self):
        t = HelmKedaTrigger(
            type="kafka",
            metadata={
                "bootstrapServers": "kafka:9092",
                "consumerGroup": "dfe-receiver",
                "lagThreshold": "100",
            },
            authentication_ref="kafka-auth",
        )
        assert t.metadata["consumerGroup"] == "dfe-receiver"


class TestHelmKedaConfig:
    def test_defaults(self):
        k = HelmKedaConfig()
        assert not k.enabled
        assert k.triggers == []

    def test_enabled_with_triggers(self):
        k = HelmKedaConfig(
            enabled=True,
            min_replicas=2,
            max_replicas=10,
            triggers=[HelmKedaTrigger(type="kafka", metadata={"lagThreshold": "100"})],
        )
        assert k.enabled
        assert len(k.triggers) == 1


class TestHelmServiceValues:
    def test_minimal(self):
        v = HelmServiceValues(image="harbor.hyperi.io/dfe/dfe-receiver")
        assert v.image_tag == "latest"
        assert v.replicas == 1
        assert v.keda.enabled is False

    def test_full(self):
        v = HelmServiceValues(
            image="harbor.hyperi.io/dfe/dfe-receiver",
            image_tag="1.2.0",
            replicas=2,
            resources={"requests": {"cpu": "500m", "memory": "1Gi"}},
            config={"server": {"bind_address": "0.0.0.0:8080"}},
            secret_refs={"config-secrets": "dfe-receiver-secrets"},
            extra_env={"LOG_LEVEL": "debug"},
        )
        assert v.replicas == 2
        assert v.config["server"]["bind_address"] == "0.0.0.0:8080"
        assert v.extra_env["LOG_LEVEL"] == "debug"

    def test_serialization(self):
        v = HelmServiceValues(image="test:latest")
        data = v.model_dump(mode="json")
        restored = HelmServiceValues.model_validate(data)
        assert restored.image == "test:latest"


class TestCompilationResult:
    def test_empty(self):
        r = CompilationResult()
        assert r.helm_values == {}
        assert r.ddl_statements == []
        assert r.kafka_topics == []
        assert r.errors == []

    def test_with_values(self):
        r = CompilationResult(
            helm_values={
                "receiver-production": HelmServiceValues(
                    image="harbor.hyperi.io/dfe/dfe-receiver"
                ),
            },
            ddl_statements=["CREATE TABLE ..."],
            kafka_topics=[{"name": "events_land", "partitions": 3}],
            warnings=["no archiver config found"],
        )
        assert "receiver-production" in r.helm_values
        assert len(r.ddl_statements) == 1
        assert len(r.kafka_topics) == 1
        assert len(r.warnings) == 1
