#  Project:      dfe-engine
#  File:         tests/unit/test_services/test_service_templates.py
#  Purpose:      Tests for generate_template() — pure logic, no external deps
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Tests for services/templates.py — pure logic only, no HTTP/external deps."""

import json

import pytest

from dfe_engine.services.plugins import reset, valid_services
from dfe_engine.services.plugins_builtin.receiver import loader_address
from dfe_engine.services.templates import generate_template
from tests.support.deployment_address import IN_CLUSTER_NAME


@pytest.fixture(autouse=True)
def _reset_plugins():
    """Ensure plugin registry is populated for each test."""
    reset()
    yield
    reset()


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


class TestGenerateTemplateErrors:
    def test_unknown_service_raises(self):
        with pytest.raises(ValueError, match="Unknown service"):
            generate_template("nonexistent-svc", profile="default")

    def test_unknown_profile_raises(self):
        with pytest.raises(ValueError, match="Unknown profile"):
            generate_template("receiver", profile="invalid-profile")

    def test_unknown_service_message_includes_valid_list(self):
        with pytest.raises(ValueError, match="receiver"):
            generate_template("no-such-service", profile="default")


# ---------------------------------------------------------------------------
# Default profile — all built-in services
# ---------------------------------------------------------------------------


class TestDefaultProfile:
    def test_returns_dict(self):
        result = generate_template("receiver", profile="default")
        assert isinstance(result, dict)

    def test_default_is_default_when_omitted(self):
        result_explicit = generate_template("receiver", profile="default")
        result_implicit = generate_template("receiver")
        assert result_explicit == result_implicit

    def test_receiver_default_has_server(self):
        result = generate_template("receiver", profile="default")
        assert "server" in result

    def test_loader_default_has_kafka(self):
        result = generate_template("loader", profile="default")
        assert "kafka" in result
        assert "brokers" in result["kafka"]

    def test_loader_default_has_clickhouse(self):
        result = generate_template("loader", profile="default")
        assert "clickhouse" in result
        assert "hosts" in result["clickhouse"]

    def test_loader_default_has_buffer(self):
        result = generate_template("loader", profile="default")
        assert "buffer" in result
        assert "flush_bytes" in result["buffer"]

    def test_archiver_default_returns_dict(self):
        result = generate_template("archiver", profile="default")
        assert isinstance(result, dict)

    def test_all_services_return_dict_for_default(self):
        for svc in valid_services():
            result = generate_template(svc, profile="default")
            assert isinstance(result, dict), f"Expected dict for {svc}"
            assert len(result) > 0, f"Expected non-empty dict for {svc}"


# ---------------------------------------------------------------------------
# Production profile
# ---------------------------------------------------------------------------


class TestProductionProfile:
    def test_receiver_production_returns_dict(self):
        result = generate_template("receiver", profile="production")
        assert isinstance(result, dict)

    def test_receiver_production_has_tls_enabled(self):
        # Production profile enables TLS
        result = generate_template("receiver", profile="production")
        assert result["server"]["tls"]["enabled"] is True

    def test_receiver_production_has_bearer_auth(self):
        result = generate_template("receiver", profile="production")
        assert result["server"]["auth"]["mode"] == "bearer"

    def test_loader_production_returns_dict(self):
        result = generate_template("loader", profile="production")
        assert isinstance(result, dict)

    def test_loader_production_has_kafka_sasl(self):
        result = generate_template("loader", profile="production")
        assert result["kafka"]["sasl"]["enabled"] is True
        assert result["kafka"]["sasl"]["mechanism"] == "scram_sha_512"

    def test_loader_production_has_kafka_tls(self):
        result = generate_template("loader", profile="production")
        assert result["kafka"]["tls"]["enabled"] is True

    def test_loader_production_has_larger_buffer(self):
        default = generate_template("loader", profile="default")
        production = generate_template("loader", profile="production")
        # Production uses 4MB flush_bytes (4_194_304) vs default 1MB (1_048_576)
        assert production["buffer"]["flush_bytes"] > default["buffer"]["flush_bytes"]

    def test_all_services_return_dict_for_production(self):
        for svc in valid_services():
            result = generate_template(svc, profile="production")
            assert isinstance(result, dict), f"Expected dict for {svc}"

    def test_default_and_production_are_different_for_receiver(self):
        default = generate_template("receiver", profile="default")
        production = generate_template("receiver", profile="production")
        # Production should have TLS enabled, default should not
        assert default != production

    def test_production_does_not_mutate_default(self):
        """Calling production profile twice must not mutate base config."""
        result1 = generate_template("loader", profile="production")
        result2 = generate_template("loader", profile="production")
        assert result1 == result2

    def test_base_not_mutated_by_production(self):
        """Generating production profile must not modify the default template."""
        default_before = generate_template("loader", profile="default")
        generate_template("loader", profile="production")
        default_after = generate_template("loader", profile="default")
        assert default_before == default_after


# ---------------------------------------------------------------------------
# k8s profile
# ---------------------------------------------------------------------------


class TestK8sProfile:
    def test_receiver_k8s_returns_dict(self):
        result = generate_template("receiver", profile="k8s")
        assert isinstance(result, dict)

    @pytest.mark.parametrize("service", ["receiver", "loader", "archiver", "fetcher"])
    def test_k8s_adds_no_broker(self, service):
        """The deployment names its brokers, so the profile keeps the app's own default."""
        k8s = generate_template(service, profile="k8s")["kafka"]["brokers"]
        assert k8s == generate_template(service, profile="default")["kafka"]["brokers"]

    def test_loader_k8s_adds_no_clickhouse_host(self):
        k8s = generate_template("loader", profile="k8s")["clickhouse"]["hosts"]
        assert k8s == generate_template("loader", profile="default")["clickhouse"]["hosts"]

    def test_receiver_k8s_dials_the_loader_the_app_manifest_places(self):
        address = generate_template("receiver", profile="k8s")["loader"]["address"]
        host, _, port = address.rpartition(":")

        assert address == loader_address()
        assert port == "6000"
        assert "." not in host, f"{address} names a namespace"

    @pytest.mark.parametrize("service", sorted(valid_services()))
    def test_k8s_names_no_in_cluster_address(self, service):
        emitted = json.dumps(generate_template(service, profile="k8s"))
        assert not IN_CLUSTER_NAME.search(emitted), f"{service} names a deployment's address"

    def test_loader_k8s_has_json_logging(self):
        result = generate_template("loader", profile="k8s")
        assert result.get("logging", {}).get("format") == "json"

    def test_all_services_return_dict_for_k8s(self):
        for svc in valid_services():
            result = generate_template(svc, profile="k8s")
            assert isinstance(result, dict), f"Expected dict for {svc}"
