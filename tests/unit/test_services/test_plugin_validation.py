#  Project:      dfe-engine
#  File:         tests/unit/test_services/test_plugin_validation.py
#  Purpose:      Tests for _validate_loader() and _validate_receiver() validation
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Tests for plugin validation functions — pure logic, no external deps."""

import pytest
from pydantic import SecretStr, ValidationError

from dfe_engine.services.models.archiver import ArchiverConfig
from dfe_engine.services.models.common import SaslConfig
from dfe_engine.services.models.fetcher import FetcherConfig, FetcherSourceConfig
from dfe_engine.services.models.loader import (
    ClickHouseConfig,
    GrpcConfig,
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

    def test_no_warnings_for_org_routes_with_db_fields(self):
        config = LoaderConfig()
        config.routing.db_fields = ["org_id"]
        config.routing.org_routes = [{"org_id": "acme"}]
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

    def test_empty_topics_and_no_regex_is_auto_discovery(self):
        # dfe-loader src/config/loader.rs validate(): an empty list discovers every
        # *_load/*_land topic, so refusing it refuses the loader's own default.
        config = LoaderConfig()
        config.kafka.topics = []
        config.kafka.topic_regex = None
        errors, _ = run_loader_validation(config)
        assert not any("topic" in e.lower() for e in errors)

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
# _validate_loader — transport scoping (kafka vs grpc)
# ---------------------------------------------------------------------------


class TestValidateLoaderTransport:
    """The Kafka requirements apply only to the Kafka transport.

    Mirrors dfe-loader/src/config/loader.rs Config::validate() scoped by
    transport, so the engine can author a gRPC-transport loader config.
    """

    def _make_grpc(self, listen: str | None = "0.0.0.0:6000") -> LoaderConfig:
        config = LoaderConfig()
        config.transport = "grpc"
        config.grpc.listen = listen
        return config

    def test_grpc_without_brokers_or_topics_passes(self):
        # The acceptance case: a grpc-only config carries no Kafka wiring at all.
        config = self._make_grpc()
        config.kafka.brokers = []
        config.kafka.topics = []
        config.kafka.topic_regex = None
        errors, _ = run_loader_validation(config)
        assert errors == []

    def test_kafka_without_brokers_still_fails(self):
        # The kafka path must keep its broker requirement.
        config = LoaderConfig()
        config.transport = "kafka"
        config.kafka.brokers = []
        errors, _ = run_loader_validation(config)
        assert any("broker" in e.lower() for e in errors)

    def test_grpc_without_listen_produces_error(self):
        # scalo fails the first recv() with "no listen address configured for
        # receiving" -- catch it at author time instead.
        config = self._make_grpc(listen=None)
        errors, _ = run_loader_validation(config)
        assert any("grpc.listen" in e for e in errors)

    def test_grpc_with_listen_passes(self):
        config = self._make_grpc()
        errors, _ = run_loader_validation(config)
        assert errors == []

    def test_a_listen_port_the_receiver_will_not_dial_is_refused(self):
        # The three-day outage: receiver dialled 6000, loader listened on 50051,
        # every record dropped behind an HTTP 202 with nothing logged.
        config = self._make_grpc(listen="0.0.0.0:50051")
        errors, _ = run_loader_validation(config)
        assert any("50051" in e and "6000" in e for e in errors)

    def test_a_listen_without_a_port_is_refused(self):
        config = self._make_grpc(listen="127.0.0.1")
        errors, _ = run_loader_validation(config)
        assert any("port number" in e for e in errors)

    def test_grpc_still_requires_clickhouse_hosts(self):
        # ClickHouse is the sink on both transports.
        config = self._make_grpc()
        config.clickhouse.hosts = []
        errors, _ = run_loader_validation(config)
        assert any("clickhouse" in e.lower() or "host" in e.lower() for e in errors)

    def test_grpc_ignores_kafka_sasl_gaps(self):
        # A half-filled SASL block is inert under grpc -- it must not error.
        config = self._make_grpc()
        config.kafka.sasl = SaslConfig(enabled=True, mechanism="scram_sha_512", username="")
        errors, _ = run_loader_validation(config)
        assert errors == []

    def test_kafka_transport_is_case_insensitive(self):
        config = LoaderConfig()
        config.transport = "KAFKA"
        config.kafka.brokers = []
        errors, _ = run_loader_validation(config)
        assert any("broker" in e.lower() for e in errors)


# ---------------------------------------------------------------------------
# LoaderConfig / GrpcConfig — model defaults
# ---------------------------------------------------------------------------


class TestLoaderTransportModelDefaults:
    """Defaults must match the Rust impl Default values exactly.

    SSoT: dfe-loader/src/config/kafka.rs GrpcConfig::default() and
    dfe-loader/src/config/loader.rs default_transport().
    """

    def test_transport_defaults_to_kafka(self):
        assert LoaderConfig().transport == "kafka"

    def test_grpc_listen_defaults_to_none(self):
        # Rust: listen: None -- no port is assumed.
        assert GrpcConfig().listen is None

    def test_grpc_defaults_mirror_rust(self):
        config = GrpcConfig()
        assert config.recv_buffer_size == 10_000
        assert config.recv_timeout_ms == 100
        assert config.max_message_size == 16 * 1024 * 1024
        assert config.compression is False
        assert config.default_topic == "main_land"

    def test_loader_config_carries_grpc_block_by_default(self):
        assert LoaderConfig().grpc.listen is None

    def test_invalid_transport_rejected(self):
        with pytest.raises(ValidationError, match="Invalid transport"):
            LoaderConfig(transport="rabbitmq")

    @pytest.mark.parametrize(
        ("given", "expected"),
        [("GRPC", "grpc"), ("Grpc", "grpc"), ("KAFKA", "kafka")],
    )
    def test_transport_is_normalised_to_lowercase(self, given, expected):
        # The loader dispatches on an EXACT match (transport.rs:546,
        # `config.transport == "grpc"`), so an authored "GRPC" would silently run
        # the Kafka path. Normalise here rather than emit a casing the binary
        # mis-dispatches.
        assert LoaderConfig(transport=given).transport == expected

    def test_grpc_transport_round_trips_through_yaml_shape(self):
        # The authored shape the engine emits for a grpc loader.
        config = LoaderConfig.model_validate(
            {"transport": "grpc", "grpc": {"listen": "0.0.0.0:6000"}}
        )
        assert config.transport == "grpc"
        assert config.grpc.listen == "0.0.0.0:6000"
        dumped = config.model_dump(mode="json", by_alias=True)
        assert dumped["transport"] == "grpc"
        assert dumped["grpc"]["listen"] == "0.0.0.0:6000"


# ---------------------------------------------------------------------------
# _validate_loader — routing warnings
# ---------------------------------------------------------------------------


class TestValidateLoaderRouting:
    def test_org_routes_without_db_fields_produces_warning(self):
        # The loader reads the org from the first db_fields hit, so org_routes
        # with no db_fields is a map it never opens.
        config = LoaderConfig()
        config.routing.db_fields = []
        config.routing.org_routes = [{"org_id": "org-a"}, {"org_id": "org-b"}]
        _, warnings = run_loader_validation(config)
        assert any("org_routes" in w for w in warnings)

    def test_db_fields_present_no_warning(self):
        config = LoaderConfig()
        config.routing.db_fields = ["org_id"]
        config.routing.org_routes = [{"org_id": "org-a"}]
        _, warnings = run_loader_validation(config)
        assert not any("org_routes" in w for w in warnings)

    def test_no_org_routes_no_warning(self):
        config = LoaderConfig()
        config.routing.db_fields = []
        config.routing.org_routes = []
        _, warnings = run_loader_validation(config)
        assert not any("org_routes" in w for w in warnings)


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
