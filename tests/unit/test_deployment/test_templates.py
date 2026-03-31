"""Tests for deployment template generation."""

from __future__ import annotations

import pytest

from dfe_engine.deployment.models.common import TShirtSize
from dfe_engine.deployment.templates import generate_template


class TestGenerateTemplate:
    def test_valid_service_default_profile(self):
        result = generate_template("receiver", profile="default")
        assert isinstance(result, dict)
        assert result["size"] == TShirtSize.xs.value
        assert result["replicas"] == 1
        assert result["keda"]["enabled"] is False
        assert result["hpa"]["enabled"] is False

    def test_valid_service_production_profile(self):
        result = generate_template("receiver", profile="production")
        assert isinstance(result, dict)
        assert result["keda"]["enabled"] is True
        assert result["hpa"]["enabled"] is False
        assert result["pod"]["annotations"]["prometheus.io/scrape"] == "true"
        assert result["pod"]["annotations"]["prometheus.io/path"] == "/metrics"

    def test_default_profile_xs_resources(self):
        result = generate_template("loader", profile="default")
        assert result["resources"]["limits"]["cpu"] == "500m"
        assert result["resources"]["limits"]["memory"] == "1Gi"
        assert result["resources"]["requests"]["cpu"] == "250m"
        assert result["resources"]["requests"]["memory"] == "512Mi"

    def test_production_profile_uses_service_default_size(self):
        # loader default size is medium (from plugin)
        result = generate_template("loader", profile="production")
        assert result["size"] == "medium"
        assert result["resources"]["limits"]["cpu"] == "2"

    def test_unknown_service_raises_value_error(self):
        with pytest.raises(ValueError, match="Unknown service"):
            generate_template("nonexistent-service", profile="default")

    def test_unknown_profile_raises_value_error(self):
        with pytest.raises(ValueError, match="Unknown profile"):
            generate_template("receiver", profile="invalid-profile")

    def test_default_profile_is_used_when_not_specified(self):
        result = generate_template("archiver")
        assert result["size"] == TShirtSize.xs.value
        assert result["keda"]["enabled"] is False

    def test_production_profile_prometheus_port(self):
        result = generate_template("receiver", profile="production")
        annotations = result["pod"]["annotations"]
        assert "prometheus.io/port" in annotations
        # The port should be a string representation of a number
        assert annotations["prometheus.io/port"].isdigit()

    def test_production_profile_keda_replicas_set(self):
        result = generate_template("loader", profile="production")
        keda = result["keda"]
        assert isinstance(keda["min_replicas"], int)
        assert isinstance(keda["max_replicas"], int)
        assert keda["max_replicas"] >= keda["min_replicas"]

    def test_all_valid_services_generate_default_template(self):
        from dfe_engine.services.plugins import valid_services

        for service in valid_services():
            result = generate_template(service, profile="default")
            assert result["size"] == TShirtSize.xs.value
            assert result["keda"]["enabled"] is False

    def test_all_valid_services_generate_production_template(self):
        from dfe_engine.services.plugins import valid_services

        for service in valid_services():
            result = generate_template(service, profile="production")
            assert result["keda"]["enabled"] is True

    def test_archiver_production_has_expected_fields(self):
        result = generate_template("archiver", profile="production")
        assert "resources" in result
        assert "keda" in result
        assert "hpa" in result
        assert "pod" in result

    def test_receiver_default_has_no_annotations(self):
        result = generate_template("receiver", profile="default")
        # Default profile does NOT set prometheus annotations
        annotations = result["pod"].get("annotations", {})
        assert "prometheus.io/scrape" not in annotations
