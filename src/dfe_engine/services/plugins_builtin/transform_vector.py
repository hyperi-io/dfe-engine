"""Built-in plugin for dfe-transform-vector.

Consolidates descriptor, validation, sizing, and template overrides
for the vector.dev-based transform service. Type 2 service (multi-source)
with instance-specific config tree (vector-only pattern).
"""

from __future__ import annotations

from typing import Any

from dfe_engine.services.descriptor import KafkaRole, ServiceDescriptor
from dfe_engine.services.plugin import ServicePlugin

descriptor = ServiceDescriptor(
    name="transform-vector",
    display_name="DFE Transform (Vector)",
    image="ghcr.io/hyperi-io/dfe-transform-vector",
    default_port=8080,
    metrics_port=9090,
    kafka_role=KafkaRole.BOTH,
    consumer_group="dfe-transform-vector",
    liveness_paths=("/livez",),
    readiness_paths=("/readyz",),
    description="Kafka-to-Kafka transform using vector.dev pipelines. Multi-source with config tree.",
)


def _validate_transform_vector(config: Any, errors: list[str], warnings: list[str]) -> None:
    """Cross-field validation for dfe-transform-vector config."""
    if not config.kafka.consumer.brokers and not config.ipc.enabled:
        errors.append("kafka.consumer.brokers is required when IPC is not enabled")

    if not config.kafka.producer.brokers and not config.ipc.enabled:
        errors.append("kafka.producer.brokers is required when IPC is not enabled")

    # Check source names are unique
    source_names = [s.name for s in config.sources]
    if len(source_names) != len(set(source_names)):
        errors.append("Source names must be unique within a deployment")

    # Check parent references are valid
    name_set = set(source_names)
    for src in config.sources:
        if src.parent and src.parent not in name_set:
            errors.append(f"Source '{src.name}' references unknown parent '{src.parent}'")


_sizing_overrides: dict[str, dict[str, Any]] = {
    "xs": {},
    "small": {},
    "medium": {
        "memory": {"limit_bytes": 2147483648},
    },
    "large": {
        "memory": {"limit_bytes": 4294967296},
    },
    "xlarge": {
        "memory": {"limit_bytes": 8589934592},
    },
}

_keda_defaults: dict[str, Any] = {
    "min_replicas": 1,
    "max_replicas": 8,
}

_template_overrides: dict[str, dict[str, Any]] = {
    "production": {
        "kafka": {
            "consumer": {
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
            "producer": {
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
    },
    "k8s": {
        "kafka": {
            "consumer": {
                "brokers": ["kafka-bootstrap.kafka.svc.cluster.local:9092"],
            },
            "producer": {
                "brokers": ["kafka-bootstrap.kafka.svc.cluster.local:9092"],
            },
        },
        "metrics": {"address": "0.0.0.0:9090"},
    },
}


def _make_plugin() -> ServicePlugin:
    from dfe_engine.deployment.models.transform_vector import (
        TransformVectorDeploymentConfig,
    )
    from dfe_engine.services.models.transform_vector import TransformVectorConfig

    return ServicePlugin(
        descriptor=descriptor,
        config_class=TransformVectorConfig,
        deployment_class=TransformVectorDeploymentConfig,
        validate_config=_validate_transform_vector,
        sizing_overrides=_sizing_overrides,
        keda_defaults=_keda_defaults,
        default_size="medium",
        config_template_overrides=_template_overrides,
    )


def plugin() -> ServicePlugin:
    return _make_plugin()
