"""Built-in plugin for dfe-receiver.

Consolidates receiver-specific descriptor, validation, sizing, and template
overrides that were previously scattered across validators.py, templates.py,
sizing.py, and state.py.
"""

from __future__ import annotations

from typing import Any

from dfe_engine.services.descriptor import KafkaRole, ServiceDescriptor
from dfe_engine.services.plugin import ServicePlugin

descriptor = ServiceDescriptor(
    name="receiver",
    display_name="DFE Receiver",
    image="ghcr.io/hyperi-io/dfe-receiver",
    default_port=8080,
    metrics_port=9090,
    kafka_role=KafkaRole.PRODUCER,
    liveness_paths=("/livez",),
    readiness_paths=("/readyz",),
    description="HTTP/gRPC ingestion gateway — receives events and routes to Kafka.",
    extra_ports={"grpc": 6000},
)


def _validate_receiver(config: Any, errors: list[str], warnings: list[str]) -> None:
    """Cross-field validation for dfe-receiver config.

    Mirrors dfe-receiver/src/config/mod.rs Config::validate().
    """
    if config.destinations.default == "kafka" and not config.kafka.brokers:
        errors.append("kafka.brokers is required when destinations.default is 'kafka'")

    if config.server.auth.mode == "bearer" and (
        not config.server.auth.bearer.tokens and not config.server.auth.bearer.secret_source
    ):
        warnings.append("auth.mode is 'bearer' but no tokens or secret_source configured")

    if config.server.auth.mode == "mtls" and not config.server.tls.enabled:
        errors.append("auth.mode is 'mtls' but TLS is not enabled")

    if config.server.auth.mode == "both" and not config.server.tls.enabled:
        errors.append("auth.mode is 'both' but TLS is not enabled")

    if config.server.tls.enabled:
        has_file = config.server.tls.cert_file and config.server.tls.key_file
        has_secret = config.server.tls.cert_secret and config.server.tls.key_secret
        if not has_file and not has_secret:
            errors.append(
                "TLS enabled but neither cert_file/key_file nor cert_secret/key_secret configured"
            )

    if config.grpc.enabled and config.grpc.bind_address == config.server.bind_address:
        errors.append("gRPC and HTTP server cannot share the same bind_address")


_sizing_overrides: dict[str, dict[str, Any]] = {
    "xs": {
        "buffer": {"memory_limit": 0, "pressure_threshold": 0.8},
    },
    "small": {
        "buffer": {"memory_limit": 0, "pressure_threshold": 0.8},
    },
    "medium": {
        "buffer": {"memory_limit": 0, "pressure_threshold": 0.8},
        "server": {"max_body_size": 10 * 1024 * 1024},
    },
    "large": {
        "buffer": {"memory_limit": 0, "pressure_threshold": 0.9},
        "server": {"max_body_size": 20 * 1024 * 1024},
    },
    "xlarge": {
        "buffer": {"memory_limit": 0, "pressure_threshold": 0.9},
        "server": {"max_body_size": 20 * 1024 * 1024},
    },
}

_keda_defaults: dict[str, Any] = {
    "min_replicas": 2,
    "max_replicas": 10,
}

_template_overrides: dict[str, dict[str, Any]] = {
    "production": {
        "server": {
            "tls": {
                "enabled": True,
                "cert_file": "/etc/tls/tls.crt",
                "key_file": "/etc/tls/tls.key",
                "ca_file": "/etc/tls/ca.crt",
                "client_auth": "none",
                "cert_secret": None,
                "key_secret": None,
                "ca_secret": None,
                "refresh_interval_secs": 3600,
            },
            "auth": {
                "mode": "bearer",
                "bearer": {
                    "secret_source": "vault:secret/dfe/receiver:bearer_tokens",
                },
            },
        },
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
            "tls": {"enabled": True},
        },
    },
    "k8s": {
        "server": {"bind_address": "0.0.0.0:8080"},
        "kafka": {"brokers": ["kafka-bootstrap.kafka.svc.cluster.local:9092"]},
        "loader": {"address": "dfe-loader.dfe.svc.cluster.local:6000"},
        "metrics": {"address": "0.0.0.0:9090"},
        "buffer": {"memory_limit": 0},
    },
}


def _make_plugin() -> ServicePlugin:
    from dfe_engine.deployment.models.receiver import ReceiverDeploymentConfig
    from dfe_engine.services.models.receiver import ReceiverConfig

    return ServicePlugin(
        descriptor=descriptor,
        config_class=ReceiverConfig,
        deployment_class=ReceiverDeploymentConfig,
        validate_config=_validate_receiver,
        sizing_overrides=_sizing_overrides,
        keda_defaults=_keda_defaults,
        default_size="small",
        config_template_overrides=_template_overrides,
    )


# Entry point callable — returns a ServicePlugin
def plugin() -> ServicePlugin:
    return _make_plugin()
