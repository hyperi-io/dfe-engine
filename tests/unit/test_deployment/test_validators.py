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

    def test_memory_decimal_si_suffixes(self):
        """K8s accepts decimal SI suffixes (k/M/G/T/P/E) as well as binary ones.

        Both families have to parse: a quantity the parser cannot read is one the
        requests-vs-limits comparison cannot make -- see
        TestUnparseableQuantityIsNotValid below.
        """
        assert _parse_k8s_quantity("1k") == 1000
        assert _parse_k8s_quantity("100M") == 100 * 1000**2
        assert _parse_k8s_quantity("2G") == 2 * 1000**3
        assert _parse_k8s_quantity("1T") == 1000**4

    def test_garbage_raises(self):
        """Unparseable input must raise, so callers cannot mistake it for zero."""
        with pytest.raises(ValueError):
            _parse_k8s_quantity("not-a-quantity")


class TestUnparseableQuantityIsNotValid:
    """A quantity the parser cannot read must never come back valid.

    ``valid`` is ``len(errors) == 0``, so reporting an unreadable quantity as a
    warning leaves the config valid while the requests-vs-limits comparison it
    blocked never runs -- "cannot determine" presented as "fine". K8s decimal SI
    suffixes are the realistic trigger.
    """

    def test_decimal_si_requests_over_limits_is_rejected(self):
        result = validate_deployment_config(
            "receiver",
            {
                "size": "custom",
                "resources": {
                    "requests": {"cpu": "500m", "memory": "8G"},
                    "limits": {"cpu": "1", "memory": "1G"},
                },
            },
        )
        assert result.valid is False, f"8G requests vs 1G limits validated clean: {result}"
        assert any("Memory requests" in e for e in result.errors), result.errors

    def test_unparseable_quantity_is_an_error_not_a_warning(self):
        result = validate_deployment_config(
            "receiver",
            {
                "size": "custom",
                "resources": {
                    "requests": {"cpu": "500m", "memory": "eight gigs"},
                    "limits": {"cpu": "1", "memory": "1Gi"},
                },
            },
        )
        assert result.valid is False, f"unparseable memory validated clean: {result}"
        assert any("Could not parse memory" in e for e in result.errors), result.errors


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
        result = validate_deployment_config(
            "receiver",
            {
                "size": "custom",
                "resources": {
                    "requests": {"cpu": "3", "memory": "6Gi"},
                    "limits": {"cpu": "6", "memory": "12Gi"},
                },
            },
        )
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

    def test_keda_without_an_explicit_trigger_is_valid(self):
        # The chart's CPU and ScalingPressure triggers apply when the config names none.
        from dfe_engine.deployment.models import ReceiverDeploymentConfig

        config = ReceiverDeploymentConfig()
        data = config.model_dump(mode="json")
        data["keda"]["enabled"] = True
        data["keda"]["kafka_trigger"] = None
        data["keda"]["cpu_trigger"] = None
        result = validate_deployment_config("receiver", data)
        assert result.valid is True, result.errors
        assert result.errors == []

    def test_resource_requests_gt_limits_error(self):
        result = validate_deployment_config(
            "receiver",
            {
                "size": "custom",
                "resources": {
                    "requests": {"cpu": "4", "memory": "8Gi"},
                    "limits": {"cpu": "2", "memory": "4Gi"},
                },
            },
        )
        assert result.valid is False
        assert any("CPU requests" in e for e in result.errors)
        assert any("Memory requests" in e for e in result.errors)

    def test_size_and_resources_warning(self):
        result = validate_deployment_config(
            "receiver",
            {
                "size": "small",
                "resources": {
                    "requests": {"cpu": "500m", "memory": "1Gi"},
                    "limits": {"cpu": "1", "memory": "2Gi"},
                },
            },
        )
        assert result.valid is True
        assert any("explicit resources" in w for w in result.warnings)

    @pytest.mark.parametrize("service", ["receiver", "loader", "archiver"])
    def test_all_service_validate_defaults(self, service):
        from dfe_engine.deployment.models import DEPLOY_CONFIG_CLASSES

        config_cls = DEPLOY_CONFIG_CLASSES[service]
        config = config_cls()
        result = validate_deployment_config(service, config.model_dump(mode="json"))
        assert result.valid is True, f"{service} default failed: {result.errors}"
