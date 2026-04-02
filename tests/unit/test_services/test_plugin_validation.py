#  Project:      dfe-engine
#  File:         tests/unit/test_services/test_plugin_validation.py
#  Purpose:      Tests for _validate_loader() and _validate_receiver() validation
#  Language:     Python
#
#  License:      FSL-1.1-ALv2
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Tests for plugin validation functions — pure logic, no external deps."""

from __future__ import annotations

from pydantic import SecretStr

from dfe_engine.services.models.archiver import ArchiverConfig
from dfe_engine.services.models.common import SaslConfig
from dfe_engine.services.models.fetcher import FetcherConfig, FetcherSourceConfig
from dfe_engine.services.models.loader import (
    ClickHouseConfig,
    LoaderBufferConfig,
    LoaderConfig,
    LoaderKafkaConfig,
    LoaderRoutingConfig,
)
from dfe_engine.services.models.receiver import ReceiverConfig
from dfe_engine.services.plugins_builtin.archiver import _validate_archiver
from dfe_engine.services.plugins_builtin.fetcher import _validate_fetcher
from dfe_engine.services.plugins_builtin.loader import _validate_loader
from dfe_engine.services.plugins_builtin.receiver import _validate_receiver

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_loader(**overrides) -> LoaderConfig:
    """Build a valid LoaderConfig, applying field overrides via model copy."""
    return LoaderConfig.model_validate(LoaderConfig().model_dump(mode="json") | overrides)


def run_loader_validation(config: LoaderConfig) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    _validate_loader(config, errors, warnings)
    return errors, warnings


def run_receiver_validation(config: ReceiverConfig) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    _validate_receiver(config, errors, warnings)
    return errors, warnings


# ---------------------------------------------------------------------------
# _validate_loader — happy path
# ---------------------------------------------------------------------------


class TestValidateLoaderHappyPath:
    def test_default_config_passes(self):
        config = LoaderConfig()
        errors, warnings = run_loader_validation(config)
        assert errors == []
        assert warnings == []

    def test_no_warnings_for_route_all_without_routed_orgs(self):
        config = LoaderConfig()
        config.routing.route_all_by_org = True
        config.routing.routed_orgs = []
        errors, warnings = run_loader_validation(config)
        assert errors == []
        assert warnings == []


# ---------------------------------------------------------------------------
# _validate_loader — broker / topic / clickhouse checks
# ---------------------------------------------------------------------------


class TestValidateLoaderBrokerTopicHosts:
    def test_empty_brokers_produces_error(self):
        config = LoaderConfig()
        config.kafka.brokers = []
        errors, _ = run_loader_validation(config)
        assert any("broker" in e.lower() for e in errors)

    def test_empty_topics_and_no_regex_produces_error(self):
        config = LoaderConfig()
        config.kafka.topics = []
        config.kafka.topic_regex = None
        errors, _ = run_loader_validation(config)
        assert any("topic" in e.lower() for e in errors)

    def test_topic_regex_satisfies_topic_requirement(self):
        config = LoaderConfig()
        config.kafka.topics = []
        config.kafka.topic_regex = "events.*"
        errors, _ = run_loader_validation(config)
        assert not any("topic" in e.lower() for e in errors)

    def test_empty_clickhouse_hosts_produces_error(self):
        config = LoaderConfig()
        config.clickhouse.hosts = []
        errors, _ = run_loader_validation(config)
        assert any("clickhouse" in e.lower() or "host" in e.lower() for e in errors)

    def test_flush_bytes_zero_produces_error(self):
        # flush_bytes has gt=0 constraint, so use model_construct to bypass
        config = LoaderConfig.model_construct(
            kafka=LoaderKafkaConfig(),
            clickhouse=ClickHouseConfig(),
            routing=LoaderRoutingConfig(),
            buffer=LoaderBufferConfig.model_construct(
                flush_bytes=0, flush_rows=10_000, flush_age_secs=5
            ),
        )
        errors, _ = run_loader_validation(config)
        assert any("flush_bytes" in e for e in errors)

    def test_flush_rows_zero_produces_error(self):
        config = LoaderConfig.model_construct(
            kafka=LoaderKafkaConfig(),
            clickhouse=ClickHouseConfig(),
            routing=LoaderRoutingConfig(),
            buffer=LoaderBufferConfig.model_construct(
                flush_bytes=1_048_576, flush_rows=0, flush_age_secs=5
            ),
        )
        errors, _ = run_loader_validation(config)
        assert any("flush_rows" in e for e in errors)


# ---------------------------------------------------------------------------
# _validate_loader — SASL validation
# ---------------------------------------------------------------------------


class TestValidateLoaderSasl:
    def _make_config_with_sasl(self, mechanism: str, **sasl_overrides) -> LoaderConfig:
        config = LoaderConfig()
        config.kafka.sasl = SaslConfig.model_validate(
            {"enabled": True, "mechanism": mechanism} | sasl_overrides
        )
        return config

    def test_sasl_disabled_no_errors(self):
        config = LoaderConfig()
        config.kafka.sasl = SaslConfig(enabled=False, mechanism="plain")
        errors, _ = run_loader_validation(config)
        assert errors == []

    def test_plain_missing_username_produces_error(self):
        config = self._make_config_with_sasl("plain", username="", password="secret")
        errors, _ = run_loader_validation(config)
        assert any("username" in e.lower() for e in errors)

    def test_plain_missing_password_produces_error(self):
        config = self._make_config_with_sasl("plain", username="user", password="")
        errors, _ = run_loader_validation(config)
        assert any("password" in e.lower() for e in errors)

    def test_plain_with_username_and_password_passes(self):
        config = self._make_config_with_sasl("plain", username="user", password="secret")
        errors, _ = run_loader_validation(config)
        assert not any("username" in e.lower() or "password" in e.lower() for e in errors)

    def test_scram_sha_256_missing_username_produces_error(self):
        config = self._make_config_with_sasl("scram_sha_256", username="", password="secret")
        errors, _ = run_loader_validation(config)
        assert any("username" in e.lower() for e in errors)

    def test_scram_sha_512_missing_password_produces_error(self):
        config = self._make_config_with_sasl("scram_sha_512", username="user", password="")
        errors, _ = run_loader_validation(config)
        assert any("password" in e.lower() for e in errors)

    def test_scram_sha_512_with_credentials_passes(self):
        config = self._make_config_with_sasl("scram_sha_512", username="user", password="secret")
        errors, _ = run_loader_validation(config)
        assert not any("username" in e.lower() or "password" in e.lower() for e in errors)

    def test_oauthbearer_missing_endpoint_produces_error(self):
        config = self._make_config_with_sasl(
            "oauthbearer",
            oauth_token_endpoint=None,
            oauth_client_id="my-client",
        )
        errors, _ = run_loader_validation(config)
        assert any("endpoint" in e.lower() for e in errors)

    def test_oauthbearer_missing_client_id_produces_error(self):
        config = self._make_config_with_sasl(
            "oauthbearer",
            oauth_token_endpoint="https://auth.example.com/token",
            oauth_client_id=None,
        )
        errors, _ = run_loader_validation(config)
        assert any("client_id" in e.lower() for e in errors)

    def test_oauthbearer_with_endpoint_and_client_id_passes(self):
        config = self._make_config_with_sasl(
            "oauthbearer",
            oauth_token_endpoint="https://auth.example.com/token",
            oauth_client_id="my-client",
        )
        errors, _ = run_loader_validation(config)
        assert not any("endpoint" in e.lower() or "client_id" in e.lower() for e in errors)

    def test_aws_msk_iam_missing_region_produces_error(self):
        config = self._make_config_with_sasl("aws_msk_iam", aws_region=None)
        errors, _ = run_loader_validation(config)
        assert any("aws_region" in e.lower() or "region" in e.lower() for e in errors)

    def test_aws_msk_iam_with_region_passes(self):
        config = self._make_config_with_sasl("aws_msk_iam", aws_region="ap-southeast-2")
        errors, _ = run_loader_validation(config)
        assert not any("region" in e.lower() for e in errors)


# ---------------------------------------------------------------------------
# _validate_loader — routing warnings
# ---------------------------------------------------------------------------


class TestValidateLoaderRouting:
    def test_route_all_by_org_with_routed_orgs_produces_warning(self):
        config = LoaderConfig()
        config.routing.route_all_by_org = True
        config.routing.routed_orgs = ["org-a", "org-b"]
        _, warnings = run_loader_validation(config)
        assert any("route_all_by_org" in w for w in warnings)

    def test_route_all_by_org_false_no_warning(self):
        config = LoaderConfig()
        config.routing.route_all_by_org = False
        config.routing.routed_orgs = ["org-a"]
        _, warnings = run_loader_validation(config)
        assert not any("route_all_by_org" in w for w in warnings)


# ---------------------------------------------------------------------------
# _validate_receiver — happy path
# ---------------------------------------------------------------------------


class TestValidateReceiverHappyPath:
    def test_default_config_passes(self):
        # Default ReceiverConfig has destinations.default='kafka' with no brokers,
        # which the validator correctly flags. Use 'loader' destination to get
        # a fully valid default config that passes without errors.
        config = ReceiverConfig()
        config.destinations.default = "loader"
        errors, warnings = run_receiver_validation(config)
        assert errors == []
        assert warnings == []


# ---------------------------------------------------------------------------
# _validate_receiver — kafka broker check
# ---------------------------------------------------------------------------


class TestValidateReceiverKafkaBrokers:
    def test_kafka_destination_without_brokers_produces_error(self):
        config = ReceiverConfig()
        config.destinations.default = "kafka"
        config.kafka.brokers = []
        errors, _ = run_receiver_validation(config)
        assert any("broker" in e.lower() for e in errors)

    def test_kafka_destination_with_brokers_passes(self):
        config = ReceiverConfig()
        config.destinations.default = "kafka"
        config.kafka.brokers = ["localhost:9092"]
        errors, _ = run_receiver_validation(config)
        assert not any("broker" in e.lower() for e in errors)

    def test_loader_destination_without_brokers_passes(self):
        config = ReceiverConfig()
        config.destinations.default = "loader"
        config.kafka.brokers = []
        errors, _ = run_receiver_validation(config)
        assert not any("broker" in e.lower() for e in errors)


# ---------------------------------------------------------------------------
# _validate_receiver — bearer auth warning
# ---------------------------------------------------------------------------


class TestValidateReceiverBearerAuth:
    def test_bearer_without_tokens_or_secret_source_produces_warning(self):
        config = ReceiverConfig()
        config.server.auth.mode = "bearer"
        config.server.auth.bearer.tokens = []
        config.server.auth.bearer.secret_source = None
        _, warnings = run_receiver_validation(config)
        assert any("bearer" in w.lower() or "token" in w.lower() for w in warnings)

    def test_bearer_with_tokens_no_warning(self):
        config = ReceiverConfig()
        config.server.auth.mode = "bearer"
        config.server.auth.bearer.tokens = [SecretStr("tok-abc")]
        config.server.auth.bearer.secret_source = None
        _, warnings = run_receiver_validation(config)
        assert not any("bearer" in w.lower() for w in warnings)

    def test_bearer_with_secret_source_no_warning(self):
        config = ReceiverConfig()
        config.server.auth.mode = "bearer"
        config.server.auth.bearer.tokens = []
        config.server.auth.bearer.secret_source = "vault:secret/tokens"
        _, warnings = run_receiver_validation(config)
        assert not any("bearer" in w.lower() for w in warnings)

    def test_none_auth_no_bearer_warning(self):
        config = ReceiverConfig()
        config.server.auth.mode = "none"
        _, warnings = run_receiver_validation(config)
        assert warnings == []


# ---------------------------------------------------------------------------
# _validate_receiver — TLS and auth mode
# ---------------------------------------------------------------------------


class TestValidateReceiverTls:
    def test_mtls_without_tls_enabled_produces_error(self):
        config = ReceiverConfig()
        config.server.auth.mode = "mtls"
        config.server.tls.enabled = False
        errors, _ = run_receiver_validation(config)
        assert any("mtls" in e.lower() or "tls" in e.lower() for e in errors)

    def test_both_without_tls_enabled_produces_error(self):
        config = ReceiverConfig()
        config.server.auth.mode = "both"
        config.server.tls.enabled = False
        errors, _ = run_receiver_validation(config)
        assert any("both" in e.lower() or "tls" in e.lower() for e in errors)

    def test_tls_enabled_without_cert_files_or_secrets_produces_error(self):
        config = ReceiverConfig()
        config.server.tls.enabled = True
        config.server.tls.cert_file = None
        config.server.tls.key_file = None
        config.server.tls.cert_secret = None
        config.server.tls.key_secret = None
        errors, _ = run_receiver_validation(config)
        assert any("tls" in e.lower() or "cert" in e.lower() for e in errors)

    def test_tls_enabled_with_cert_files_passes(self):
        config = ReceiverConfig()
        config.server.tls.enabled = True
        config.server.tls.cert_file = "/etc/tls/tls.crt"
        config.server.tls.key_file = "/etc/tls/tls.key"
        errors, _ = run_receiver_validation(config)
        assert not any("cert" in e.lower() for e in errors)

    def test_tls_enabled_with_secrets_passes(self):
        config = ReceiverConfig()
        config.server.tls.enabled = True
        config.server.tls.cert_file = None
        config.server.tls.key_file = None
        config.server.tls.cert_secret = "my-cert-secret"
        config.server.tls.key_secret = "my-key-secret"
        errors, _ = run_receiver_validation(config)
        assert not any("cert" in e.lower() for e in errors)

    def test_mtls_with_tls_enabled_and_certs_passes(self):
        config = ReceiverConfig()
        config.server.auth.mode = "mtls"
        config.server.tls.enabled = True
        config.server.tls.cert_file = "/etc/tls/tls.crt"
        config.server.tls.key_file = "/etc/tls/tls.key"
        errors, _ = run_receiver_validation(config)
        assert not any("mtls" in e.lower() for e in errors)


# ---------------------------------------------------------------------------
# _validate_receiver — gRPC bind address conflict
# ---------------------------------------------------------------------------


class TestValidateReceiverGrpc:
    def test_grpc_same_address_as_http_produces_error(self):
        config = ReceiverConfig()
        config.grpc.enabled = True
        config.grpc.bind_address = "0.0.0.0:8080"
        config.server.bind_address = "0.0.0.0:8080"
        errors, _ = run_receiver_validation(config)
        assert any("bind_address" in e.lower() or "grpc" in e.lower() for e in errors)

    def test_grpc_different_address_passes(self):
        config = ReceiverConfig()
        config.grpc.enabled = True
        config.grpc.bind_address = "0.0.0.0:6000"
        config.server.bind_address = "0.0.0.0:8080"
        errors, _ = run_receiver_validation(config)
        assert not any("bind_address" in e.lower() for e in errors)

    def test_grpc_disabled_same_address_no_error(self):
        config = ReceiverConfig()
        config.grpc.enabled = False
        config.grpc.bind_address = "0.0.0.0:8080"
        config.server.bind_address = "0.0.0.0:8080"
        errors, _ = run_receiver_validation(config)
        assert not any("bind_address" in e.lower() for e in errors)


# ---------------------------------------------------------------------------
# _validate_archiver — validation logic
# ---------------------------------------------------------------------------


def run_archiver_validation(config: ArchiverConfig) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    _validate_archiver(config, errors, warnings)
    return errors, warnings


class TestValidateArchiverHappyPath:
    def test_default_config_passes(self):
        # Default destination is file:// which is valid; default kafka topics
        # is empty which produces a warning but no error.
        config = ArchiverConfig()
        config.kafka.brokers = ["localhost:9092"]
        errors, _ = run_archiver_validation(config)
        assert errors == []

    def test_no_topics_produces_warning(self):
        config = ArchiverConfig()
        config.kafka.brokers = ["localhost:9092"]
        config.kafka.topics = []
        _, warnings = run_archiver_validation(config)
        assert any("topic" in w.lower() for w in warnings)


class TestValidateArchiverBrokers:
    def test_empty_brokers_produces_error(self):
        config = ArchiverConfig()
        config.kafka.brokers = []
        errors, _ = run_archiver_validation(config)
        assert any("broker" in e.lower() for e in errors)

    def test_with_brokers_no_broker_error(self):
        config = ArchiverConfig()
        config.kafka.brokers = ["localhost:9092"]
        errors, _ = run_archiver_validation(config)
        assert not any("broker" in e.lower() for e in errors)


class TestValidateArchiverDestination:
    def test_invalid_destination_scheme_produces_error(self):
        config = ArchiverConfig()
        config.kafka.brokers = ["localhost:9092"]
        config.archive.destination = "http://invalid"
        errors, _ = run_archiver_validation(config)
        assert any("destination" in e.lower() or "scheme" in e.lower() for e in errors)

    def test_s3_without_s3_config_produces_warning(self):
        config = ArchiverConfig()
        config.kafka.brokers = ["localhost:9092"]
        config.archive.destination = "s3://my-bucket/path"
        config.archive.s3 = None
        _, warnings = run_archiver_validation(config)
        assert any("s3" in w.lower() for w in warnings)

    def test_gcs_without_gcs_config_produces_warning(self):
        config = ArchiverConfig()
        config.kafka.brokers = ["localhost:9092"]
        config.archive.destination = "gs://my-bucket/path"
        config.archive.gcs = None
        _, warnings = run_archiver_validation(config)
        assert any("gcs" in w.lower() for w in warnings)

    def test_azure_without_azure_config_produces_warning(self):
        config = ArchiverConfig()
        config.kafka.brokers = ["localhost:9092"]
        config.archive.destination = "az://my-container/path"
        config.archive.azure = None
        _, warnings = run_archiver_validation(config)
        assert any("azure" in w.lower() for w in warnings)

    def test_minio_without_minio_config_produces_warning(self):
        config = ArchiverConfig()
        config.kafka.brokers = ["localhost:9092"]
        config.archive.destination = "minio://my-bucket/path"
        config.archive.minio = None
        _, warnings = run_archiver_validation(config)
        assert any("minio" in w.lower() for w in warnings)


class TestValidateArchiverSasl:
    def test_sasl_security_protocol_without_mechanism_produces_error(self):
        config = ArchiverConfig()
        config.kafka.brokers = ["localhost:9092"]
        config.kafka.security_protocol = "SASL_SSL"
        config.kafka.sasl_mechanism = None
        errors, _ = run_archiver_validation(config)
        assert any("sasl_mechanism" in e.lower() or "mechanism" in e.lower() for e in errors)

    def test_sasl_plaintext_without_mechanism_produces_error(self):
        config = ArchiverConfig()
        config.kafka.brokers = ["localhost:9092"]
        config.kafka.security_protocol = "SASL_PLAINTEXT"
        config.kafka.sasl_mechanism = None
        errors, _ = run_archiver_validation(config)
        assert any("sasl_mechanism" in e.lower() or "mechanism" in e.lower() for e in errors)

    def test_sasl_ssl_with_mechanism_passes(self):
        config = ArchiverConfig()
        config.kafka.brokers = ["localhost:9092"]
        config.kafka.security_protocol = "SASL_SSL"
        config.kafka.sasl_mechanism = "SCRAM-SHA-512"
        errors, _ = run_archiver_validation(config)
        assert not any("mechanism" in e.lower() for e in errors)


class TestValidateArchiverRouting:
    def test_expression_mode_without_fields_produces_error(self):
        config = ArchiverConfig()
        config.kafka.brokers = ["localhost:9092"]
        config.routing.mode = "expression"
        config.routing.expression_fields = []
        errors, _ = run_archiver_validation(config)
        assert any("expression" in e.lower() for e in errors)

    def test_expression_mode_with_fields_passes(self):
        config = ArchiverConfig()
        config.kafka.brokers = ["localhost:9092"]
        config.routing.mode = "expression"
        config.routing.expression_fields = ["org_id", "source"]
        errors, _ = run_archiver_validation(config)
        assert not any("expression" in e.lower() for e in errors)


# ---------------------------------------------------------------------------
# _validate_fetcher — validation logic
# ---------------------------------------------------------------------------


def run_fetcher_validation(config: FetcherConfig) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    _validate_fetcher(config, errors, warnings)
    return errors, warnings


class TestValidateFetcherHappyPath:
    def test_no_brokers_produces_error(self):
        config = FetcherConfig()
        config.kafka.brokers = []
        errors, _ = run_fetcher_validation(config)
        assert any("broker" in e.lower() for e in errors)

    def test_with_brokers_no_broker_error(self):
        config = FetcherConfig()
        config.kafka.brokers = ["localhost:9092"]
        errors, _ = run_fetcher_validation(config)
        assert not any("broker" in e.lower() for e in errors)


class TestValidateFetcherSources:
    def test_duplicate_source_names_produces_error(self):
        config = FetcherConfig()
        config.kafka.brokers = ["localhost:9092"]
        src_a = FetcherSourceConfig(name="my-source", source_type="api")
        src_b = FetcherSourceConfig(name="my-source", source_type="api")
        config.sources = [src_a, src_b]
        errors, _ = run_fetcher_validation(config)
        assert any("unique" in e.lower() for e in errors)

    def test_source_missing_source_type_produces_error(self):
        config = FetcherConfig()
        config.kafka.brokers = ["localhost:9092"]
        src = FetcherSourceConfig.model_construct(name="test", source_type=None)
        config.sources = [src]
        errors, _ = run_fetcher_validation(config)
        assert any("source_type" in e.lower() or "missing" in e.lower() for e in errors)

    def test_oauth2_without_token_url_produces_warning(self):
        config = FetcherConfig()
        config.kafka.brokers = ["localhost:9092"]
        # Default auth type is oauth2 with no token_url
        src = FetcherSourceConfig(name="api-src", source_type="rest_api")
        config.sources = [src]
        _, warnings = run_fetcher_validation(config)
        assert any("oauth2" in w.lower() or "token_url" in w.lower() for w in warnings)
