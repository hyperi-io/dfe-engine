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

from dfe_engine.appmgmt import catalogue
from dfe_engine.services.plugins import reset, valid_services
from dfe_engine.services.templates import generate_template
from tests.support.deployment_address import IN_CLUSTER_NAME

# Every list naming where a service's brokers or ClickHouse live, as a dotted path.
ADDRESS_LISTS = [
    ("receiver", "kafka.brokers"),
    ("loader", "kafka.brokers"),
    ("loader", "clickhouse.hosts"),
    ("archiver", "kafka.brokers"),
    ("fetcher", "kafka.brokers"),
    ("transform-vector", "kafka.consumer.brokers"),
    ("transform-vector", "kafka.producer.brokers"),
    ("transform-wasm", "kafka.consumer.brokers"),
    ("transform-wasm", "kafka.producer.brokers"),
    ("transform-vrl", "source.brokers"),
    ("transform-vrl", "sink.brokers"),
]


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

    @pytest.mark.parametrize(("service", "path"), ADDRESS_LISTS)
    def test_k8s_empties_every_broker_and_host_list(self, service, path):
        """The deployment names its own, so the profile emits an empty list."""
        value = generate_template(service, profile="k8s")
        for key in path.split("."):
            value = value[key]
        assert value == []

    def test_an_empty_list_replaces_the_app_s_localhost_default(self):
        default = generate_template("loader", profile="default")
        k8s = generate_template("loader", profile="k8s")

        assert default["kafka"]["brokers"] == ["localhost:9092"]
        assert default["clickhouse"]["hosts"] == ["localhost:8123"]
        assert k8s["kafka"]["brokers"] == []
        assert k8s["clickhouse"]["hosts"] == []

    @pytest.mark.parametrize("service", sorted(valid_services()))
    def test_no_localhost_survives_the_k8s_profile(self, service):
        emitted = json.dumps(generate_template(service, profile="k8s"))
        assert "localhost" not in emitted, f"{service}'s k8s template still names localhost"

    def test_receiver_k8s_dials_the_loader_the_app_manifest_places(self):
        address = generate_template("receiver", profile="k8s")["loader"]["address"]
        host, _, port = address.rpartition(":")

        assert address == catalogue.push_address(catalogue.descriptor("dfe-loader"))
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
