"""Tests for Helm values models (chart-shaped overlay schema)."""

from dfe_engine.helm.models import (
    CompilationResult,
    HelmDeployMeta,
    HelmImage,
    HelmKeda,
    HelmKedaTrigger,
    HelmServiceValues,
)


def _meta() -> HelmDeployMeta:
    return HelmDeployMeta(service="dfe-receiver", instance="production")


class TestHelmKedaTrigger:
    def test_defaults(self):
        t = HelmKedaTrigger()
        assert t.type == "metrics-api"
        assert t.metadata == {}
        assert t.authenticationRef is None

    def test_kafka_trigger(self):
        t = HelmKedaTrigger(
            type="kafka",
            metadata={
                "bootstrapServers": "kafka:9092",
                "consumerGroup": "dfe-receiver",
                "lagThreshold": "100",
            },
            authenticationRef={"name": "dfe-receiver-trigger-auth"},
        )
        assert t.metadata["consumerGroup"] == "dfe-receiver"
        assert t.authenticationRef["name"] == "dfe-receiver-trigger-auth"


class TestHelmKeda:
    def test_defaults(self):
        k = HelmKeda()
        assert not k.enabled
        # triggers default None so the overlay omits it (chart default stands).
        assert k.triggers is None

    def test_enabled_bounds_only(self):
        k = HelmKeda(enabled=True, minReplicaCount=2, maxReplicaCount=10)
        assert k.enabled
        assert k.minReplicaCount == 2
        assert k.triggers is None


class TestHelmServiceValues:
    def test_minimal(self):
        v = HelmServiceValues(deploy=_meta())
        assert v.replicaCount == 1
        assert v.image.pullPolicy == "IfNotPresent"
        assert v.keda.enabled is False
        assert v.deploy.service == "dfe-receiver"

    def test_full(self):
        v = HelmServiceValues(
            deploy=_meta(),
            image=HelmImage(repository="ghcr.io/hyperi-io/dfe-receiver", tag="1.2.0"),
            replicaCount=2,
            resources={"requests": {"cpu": "500m", "memory": "1Gi"}},
            nodeScheduling={"nodeSelector": {"dfe.hyperi.io/pool": "dfe"}},
            config={"server": {"bind_address": "0.0.0.0:8080"}},
            secret_refs={"config-secrets": "dfe-receiver-secrets"},
            extra_env={"LOG_LEVEL": "debug"},
        )
        assert v.replicaCount == 2
        assert v.image.tag == "1.2.0"
        assert v.config["server"]["bind_address"] == "0.0.0.0:8080"
        assert v.nodeScheduling["nodeSelector"]["dfe.hyperi.io/pool"] == "dfe"

    def test_serialization_omits_none_keda_triggers(self):
        v = HelmServiceValues(deploy=_meta())
        data = v.model_dump(mode="json", exclude_none=True)
        # triggers None -> omitted so it can't clobber the chart's default trigger.
        assert "triggers" not in data["keda"]
        restored = HelmServiceValues.model_validate(data)
        assert restored.keda.triggers is None


class TestCompilationResult:
    def test_empty(self):
        r = CompilationResult()
        assert r.helm_values == {}
        assert r.ddl_statements == []
        assert r.kafka_topics == []
        assert r.errors == []

    def test_with_values(self):
        r = CompilationResult(
            helm_values={"receiver-production": HelmServiceValues(deploy=_meta())},
            ddl_statements=["CREATE TABLE ..."],
            kafka_topics=[{"name": "events_land", "partitions": 3}],
            warnings=["no archiver config found"],
        )
        assert "receiver-production" in r.helm_values
        assert len(r.ddl_statements) == 1
        assert len(r.kafka_topics) == 1
        assert len(r.warnings) == 1
