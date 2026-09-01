"""Built-in plugin for dfe-loader.

Consolidates loader-specific descriptor, validation, sizing, and template
overrides.
"""

from __future__ import annotations

from typing import Any

from dfe_engine.services.descriptor import KafkaRole, ServiceDescriptor
from dfe_engine.services.plugin import ServicePlugin

descriptor = ServiceDescriptor(
    name="loader",
    display_name="DFE Loader",
    image="harbor.hyperi.io/dfe/dfe-loader",
    default_port=9000,
    metrics_port=9090,
    kafka_role=KafkaRole.CONSUMER,
    consumer_group="clickhouse-loader",
    liveness_paths=("/livez",),
    readiness_paths=("/readyz",),
    description="Kafka consumer or gRPC server -- loads events into ClickHouse.",
    # Bound only when transport is 'grpc': the loader listens for Push RPCs from
    # dfe-receiver. Port 6000 per the dfe-loader GrpcConfig.listen doc example
    # (src/config/kafka.rs:80) and dfe-receiver's own loader endpoint example
    # (src/error.rs:257, "grpc://loader.internal:6000").
    extra_ports={"grpc": 6000},
)


def _validate_loader(config: Any, errors: list[str], warnings: list[str]) -> None:
    """Cross-field validation for dfe-loader config.

    Mirrors dfe-loader/src/config/loader.rs Config::validate(), scoped by the
    transport the config selects.

    The Rust validate() checks kafka.brokers unconditionally, but that check is
    unreachable for a gRPC deployment: serde fills kafka.brokers from
    KafkaConfig::default() (["localhost:9092"]), so a grpc-transport YAML never
    presents an empty broker list to the binary. The engine AUTHORS configs, so
    the broker requirement is scoped to the transport that actually consumes it
    -- otherwise a legitimate grpc-only config is rejected at author time.

    The grpc.listen requirement is not in the Rust validate() either, but scalo
    fails the first recv() with "no listen address configured for receiving"
    (scalo-rs/src/transport/grpc/mod.rs:652-655) when listen is unset. Catching
    it here turns a runtime pod failure into an author-time error.
    """
    transport = config.transport.lower()

    if transport == "kafka":
        if not config.kafka.brokers:
            errors.append("At least one Kafka broker must be configured")

        if not config.kafka.topics and not config.kafka.topic_regex:
            errors.append("Either kafka.topics or kafka.topic_regex must be configured")

        if config.kafka.sasl and config.kafka.sasl.enabled:
            mechanism = config.kafka.sasl.mechanism.lower().replace("-", "_")
            if mechanism in ("plain", "scram_sha_256", "scram_sha_512"):
                if not config.kafka.sasl.username:
                    errors.append(f"SASL {mechanism} requires username")
                if not config.kafka.sasl.password.get_secret_value():
                    errors.append(f"SASL {mechanism} requires password")
            elif mechanism == "oauthbearer":
                if not config.kafka.sasl.oauth_token_endpoint:
                    errors.append("SASL OAUTHBEARER requires oauth_token_endpoint")
                if not config.kafka.sasl.oauth_client_id:
                    errors.append("SASL OAUTHBEARER requires oauth_client_id")
            elif mechanism == "aws_msk_iam":
                if not config.kafka.sasl.aws_region:
                    errors.append("SASL AWS_MSK_IAM requires aws_region")

    elif transport == "grpc":
        if not config.grpc.listen:
            errors.append("grpc.listen is required when transport is 'grpc'")

    if not config.clickhouse.hosts:
        errors.append("At least one ClickHouse host must be configured")

    if config.buffer.flush_bytes == 0:
        errors.append("buffer.flush_bytes must be greater than 0")

    if config.buffer.flush_rows == 0:
        errors.append("buffer.flush_rows must be greater than 0")

    # The loader resolves the database by looking the FIRST db_fields hit up in
    # org_routes (dfe-loader/src/routing/router.rs:240-252) -- with db_fields
    # empty it short-circuits to default_db and never consults the map at all.
    if config.routing.org_routes and not config.routing.db_fields:
        warnings.append(
            "routing.org_routes is set but routing.db_fields is empty -- the loader "
            "reads the org from the first db_fields hit, so every route is dead and "
            "all data lands in default_db"
        )


_sizing_overrides: dict[str, dict[str, Any]] = {
    "xs": {
        "buffer": {"flush_bytes": 524_288, "flush_rows": 1_000},
        "memory": {"limit_bytes": 0},
    },
    "small": {
        "buffer": {"flush_bytes": 1_048_576, "flush_rows": 10_000},
        "memory": {"limit_bytes": 0},
    },
    "medium": {
        "buffer": {"flush_bytes": 4_194_304, "flush_rows": 50_000},
        "memory": {"limit_bytes": 0},
    },
    "large": {
        "buffer": {"flush_bytes": 8_388_608, "flush_rows": 100_000},
        "memory": {"limit_bytes": 0},
    },
    "xlarge": {
        "buffer": {"flush_bytes": 16_777_216, "flush_rows": 200_000},
        "memory": {"limit_bytes": 0},
    },
}

_keda_defaults: dict[str, Any] = {
    "min_replicas": 2,
    "max_replicas": 8,
    "kafka_trigger": {
        "consumer_group": "clickhouse-loader",
        "lag_threshold": 50000,
    },
}

_template_overrides: dict[str, dict[str, Any]] = {
    "production": {
        "kafka": {
            "sasl": {
                "enabled": True,
                "mechanism": "scram_sha_512",
                "username": "",
                "password": "",
                "oauth_token_endpoint": None,
                "oauth_client_id": None,
                "oauth_client_secret": None,
                "oauth_scope": None,
                "oauth_extensions": None,
                "aws_region": None,
                "aws_access_key_id": None,
                "aws_secret_access_key": None,
                "aws_session_token": None,
                "aws_profile": None,
            },
            "tls": {
                "enabled": True,
                "ca_file": "/etc/tls/kafka-ca.crt",
                "cert_file": None,
                "key_file": None,
                "skip_verify": False,
            },
        },
        "buffer": {"flush_bytes": 4_194_304, "flush_rows": 50_000},
        "logging": {"format": "json"},
    },
    "k8s": {
        "kafka": {"brokers": ["kafka-bootstrap.kafka.svc.cluster.local:9092"]},
        "clickhouse": {"hosts": ["clickhouse.clickhouse.svc.cluster.local:9000"]},
        "metrics": {"address": "0.0.0.0:9090"},
        "memory": {"limit_bytes": 0},
        "logging": {"format": "json"},
    },
}


def _make_plugin() -> ServicePlugin:
    from dfe_engine.deployment.models.loader import LoaderDeploymentConfig
    from dfe_engine.services.models.loader import LoaderConfig

    return ServicePlugin(
        descriptor=descriptor,
        config_class=LoaderConfig,
        deployment_class=LoaderDeploymentConfig,
        validate_config=_validate_loader,
        sizing_overrides=_sizing_overrides,
        keda_defaults=_keda_defaults,
        default_size="medium",
        config_template_overrides=_template_overrides,
    )


def plugin() -> ServicePlugin:
    return _make_plugin()
