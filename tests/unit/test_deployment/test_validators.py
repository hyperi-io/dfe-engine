"""Tests for deployment configuration validators."""

import pytest

from dfe_engine.deployment.validators import (
    _parse_k8s_quantity,
    validate_deployment_config,
)


class TestParseK8sQuantity:
    def test_cpu_millicores(self):
        assert _parse_k8s_quantity("500m") == 0.5
        assert _parse_k8s_quantity("250m") == 0.25
        assert _parse_k8s_quantity("1000m") == 1.0

    def test_cpu_cores(self):
        assert _parse_k8s_quantity("1") == 1.0
        assert _parse_k8s_quantity("2") == 2.0
        assert _parse_k8s_quantity("4") == 4.0

    def test_memory_mi(self):
        assert _parse_k8s_quantity("512Mi") == 512 * 1024**2

    def test_memory_gi(self):
        assert _parse_k8s_quantity("1Gi") == 1024**3
        assert _parse_k8s_quantity("2Gi") == 2 * 1024**3

    def test_memory_ki(self):
        assert _parse_k8s_quantity("1024Ki") == 1024 * 1024


class TestValidateDeploymentConfig:
    def test_valid_default_config(self):
        from dfe_engine.deployment.models import ReceiverDeploymentConfig

        config = ReceiverDeploymentConfig()
        result = validate_deployment_config("receiver", config.model_dump(mode="json"))
        assert result.valid is True
        assert result.errors == []

    def test_unknown_service(self):
        result = validate_deployment_config("unknown", {})
        assert result.valid is False
        assert "Unknown service" in result.errors[0]

    def test_invalid_schema(self):
        result = validate_deployment_config("receiver", {"bad_field": True})
        assert result.valid is False
        assert "Schema validation failed" in result.errors[0]

    def test_custom_size_without_resources(self):
        from dfe_engine.deployment.models import ReceiverDeploymentConfig

        config = ReceiverDeploymentConfig(size="custom")
        result = validate_deployment_config("receiver", config.model_dump(mode="json"))
        assert result.valid is False
        assert any("custom" in e and "resources" in e for e in result.errors)

    def test_custom_size_with_resources(self):
        result = validate_deployment_config("receiver", {
            "size": "custom",
            "resources": {
                "requests": {"cpu": "3", "memory": "6Gi"},
                "limits": {"cpu": "6", "memory": "12Gi"},
            },
        })
        assert result.valid is True

    def test_keda_and_hpa_both_enabled(self):
        from dfe_engine.deployment.models import ReceiverDeploymentConfig

        config = ReceiverDeploymentConfig()
        data = config.model_dump(mode="json")
        data["keda"]["enabled"] = True
        data["hpa"]["enabled"] = True
        result = validate_deployment_config("receiver", data)
        assert result.valid is False
        assert any("KEDA and HPA" in e for e in result.errors)

    def test_keda_min_gt_max(self):
        from dfe_engine.deployment.models import ReceiverDeploymentConfig

        config = ReceiverDeploymentConfig()
        data = config.model_dump(mode="json")
        data["keda"]["enabled"] = True
        data["keda"]["min_replicas"] = 10
        data["keda"]["max_replicas"] = 2
        result = validate_deployment_config("receiver", data)
        assert result.valid is False
        assert any("min_replicas" in e for e in result.errors)

    def test_keda_no_triggers(self):
        from dfe_engine.deployment.models import ReceiverDeploymentConfig

        config = ReceiverDeploymentConfig()
        data = config.model_dump(mode="json")
        data["keda"]["enabled"] = True
        data["keda"]["kafka_trigger"] = None
        data["keda"]["cpu_trigger"] = None
        result = validate_deployment_config("receiver", data)
        assert result.valid is False
        assert any("no triggers" in e for e in result.errors)

    def test_resource_requests_gt_limits_error(self):
        result = validate_deployment_config("receiver", {
            "size": "custom",
            "resources": {
                "requests": {"cpu": "4", "memory": "8Gi"},
                "limits": {"cpu": "2", "memory": "4Gi"},
            },
        })
        assert result.valid is False
        assert any("CPU requests" in e for e in result.errors)
        assert any("Memory requests" in e for e in result.errors)

    def test_size_and_resources_warning(self):
        result = validate_deployment_config("receiver", {
            "size": "small",
            "resources": {
                "requests": {"cpu": "500m", "memory": "1Gi"},
                "limits": {"cpu": "1", "memory": "2Gi"},
            },
        })
        assert result.valid is True
        assert any("explicit resources" in w for w in result.warnings)

    @pytest.mark.parametrize("service", ["receiver", "loader", "archiver"])
    def test_all_services_validate_defaults(self, service):
        from dfe_engine.deployment.models import DEPLOY_CONFIG_CLASSES

        config_cls = DEPLOY_CONFIG_CLASSES[service]
        config = config_cls()
        result = validate_deployment_config(service, config.model_dump(mode="json"))
        assert result.valid is True, f"{service} default failed: {result.errors}"
