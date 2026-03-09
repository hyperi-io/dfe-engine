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
    liveness_paths=("/live", "/health"),
    readiness_paths=("/ready", "/health"),
    description="Kafka consumer — loads events into ClickHouse.",
)


def _validate_loader(config: Any, errors: list[str], warnings: list[str]) -> None:
    """Cross-field validation for dfe-loader config.

    Mirrors dfe-loader/src/config/loader.rs Config::validate().
    """
    if not config.kafka.brokers:
        errors.append("At least one Kafka broker must be configured")

    if not config.kafka.topics and not config.kafka.topic_regex:
        errors.append("Either kafka.topics or kafka.topic_regex must be configured")

    if not config.clickhouse.hosts:
        errors.append("At least one ClickHouse host must be configured")

    if config.buffer.flush_bytes == 0:
        errors.append("buffer.flush_bytes must be greater than 0")

    if config.buffer.flush_rows == 0:
        errors.append("buffer.flush_rows must be greater than 0")

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

    if config.routing.route_all_by_org and config.routing.routed_orgs:
        warnings.append("route_all_by_org is true, routed_orgs list will be ignored")


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
