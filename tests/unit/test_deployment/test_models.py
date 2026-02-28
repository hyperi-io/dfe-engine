"""Tests for deployment configuration models."""

import pytest

from dfe_engine.deployment.models import (
    DEPLOY_CONFIG_CLASSES,
    VALID_DEPLOY_SERVICES,
    ArchiverDeploymentConfig,
    LoaderDeploymentConfig,
    ReceiverDeploymentConfig,
)
from dfe_engine.deployment.models.common import (
    HpaConfig,
    K8sServiceConfig,
    KedaConfig,
    KedaTriggerCpu,
    KedaTriggerKafka,
    PodConfig,
    ResourceQuantity,
    ResourceSpec,
    TShirtSize,
)


class TestTShirtSize:
    def test_enum_values(self):
        assert TShirtSize.xs.value == "xs"
        assert TShirtSize.small.value == "small"
        assert TShirtSize.medium.value == "medium"
        assert TShirtSize.large.value == "large"
        assert TShirtSize.xlarge.value == "xlarge"
        assert TShirtSize.custom.value == "custom"

    def test_from_string(self):
        assert TShirtSize("xs") == TShirtSize.xs
        assert TShirtSize("medium") == TShirtSize.medium

    def test_invalid_size(self):
        with pytest.raises(ValueError):
            TShirtSize("xxl")


class TestResourceModels:
    def test_resource_quantity(self):
        rq = ResourceQuantity(cpu="500m", memory="1Gi")
        assert rq.cpu == "500m"
        assert rq.memory == "1Gi"

    def test_resource_spec(self):
        spec = ResourceSpec(
            requests=ResourceQuantity(cpu="500m", memory="1Gi"),
            limits=ResourceQuantity(cpu="1", memory="2Gi"),
        )
        assert spec.requests.cpu == "500m"
        assert spec.limits.memory == "2Gi"

    def test_resource_spec_roundtrip(self):
        spec = ResourceSpec(
            requests=ResourceQuantity(cpu="1", memory="2Gi"),
            limits=ResourceQuantity(cpu="2", memory="4Gi"),
        )
        data = spec.model_dump()
        restored = ResourceSpec.model_validate(data)
        assert restored == spec


class TestKedaConfig:
    def test_defaults(self):
        keda = KedaConfig()
        assert keda.enabled is False
        assert keda.min_replicas == 1
        assert keda.max_replicas == 4
        assert keda.polling_interval == 30
        assert keda.cooldown_period == 300

    def test_kafka_trigger(self):
        trigger = KedaTriggerKafka(
            consumer_group="test-group",
            topic="events",
            lag_threshold=5000,
        )
        assert trigger.lag_threshold == 5000

    def test_cpu_trigger(self):
        trigger = KedaTriggerCpu(metric_type="Utilization", value=70)
        assert trigger.value == 70

    def test_cpu_trigger_invalid_type(self):
        with pytest.raises(ValueError, match="Invalid metric_type"):
            KedaTriggerCpu(metric_type="Average", value=70)


class TestHpaConfig:
    def test_defaults(self):
        hpa = HpaConfig()
        assert hpa.enabled is False
        assert hpa.min_replicas == 1
        assert hpa.max_replicas == 4
        assert hpa.target_cpu_percent == 80


class TestK8sServiceConfig:
    def test_defaults(self):
        svc = K8sServiceConfig()
        assert svc.type == "ClusterIP"
        assert svc.port == 8080

    def test_invalid_type(self):
        with pytest.raises(ValueError, match="Invalid service type"):
            K8sServiceConfig(type="ExternalName")


class TestPodConfig:
    def test_defaults_empty(self):
        pod = PodConfig()
        assert pod.annotations == {}
        assert pod.labels == {}
        assert pod.tolerations == []

    def test_with_annotations(self):
        pod = PodConfig(annotations={"prometheus.io/scrape": "true"})
        assert pod.annotations["prometheus.io/scrape"] == "true"


class TestPerServiceModels:
    @pytest.mark.parametrize("service", ["receiver", "loader", "archiver"])
    def test_default_construction(self, service):
        config_cls = DEPLOY_CONFIG_CLASSES[service]
        config = config_cls()
        assert config.image_tag == "latest"
        assert config.replicas >= 0

    @pytest.mark.parametrize("service", ["receiver", "loader", "archiver"])
    def test_roundtrip_serialization(self, service):
        config_cls = DEPLOY_CONFIG_CLASSES[service]
        config = config_cls()
        data = config.model_dump(mode="json")
        restored = config_cls.model_validate(data)
        assert restored.size == config.size
        assert restored.image == config.image

    def test_receiver_defaults(self):
        config = ReceiverDeploymentConfig()
        assert config.size == TShirtSize.small
        assert config.service.port == 8080

    def test_loader_defaults(self):
        config = LoaderDeploymentConfig()
        assert config.size == TShirtSize.medium
        assert config.service.port == 9000

    def test_archiver_defaults(self):
        config = ArchiverDeploymentConfig()
        assert config.size == TShirtSize.small
        assert config.service.port == 8080

    def test_extra_forbid(self):
        with pytest.raises(ValueError):
            ReceiverDeploymentConfig(nonexistent_field="value")

    def test_config_classes_complete(self):
        assert set(DEPLOY_CONFIG_CLASSES.keys()) == VALID_DEPLOY_SERVICES

    def test_custom_size_with_resources(self):
        config = ReceiverDeploymentConfig(
            size=TShirtSize.custom,
            resources=ResourceSpec(
                requests=ResourceQuantity(cpu="3", memory="6Gi"),
                limits=ResourceQuantity(cpu="6", memory="12Gi"),
            ),
        )
        assert config.size == TShirtSize.custom
        assert config.resources.limits.cpu == "6"
