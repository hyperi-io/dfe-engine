"""Built-in plugin for dfe-transform-vector.

Consolidates descriptor, validation, and template overrides
for the vector.dev-based transform service. Type 2 service (multi-source)
with instance-specific config tree (vector-only pattern).
"""

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
        # Empty: every deployment names its own brokers.
        "kafka": {"consumer": {"brokers": []}, "producer": {"brokers": []}},
        "metrics": {"address": "0.0.0.0:9090"},
    },
}


def _make_plugin() -> ServicePlugin:
    from dfe_engine.services.models.transform_vector import TransformVectorConfig

    return ServicePlugin(
        descriptor=descriptor,
        config_class=TransformVectorConfig,
        validate_config=_validate_transform_vector,
        config_template_overrides=_template_overrides,
    )


def plugin() -> ServicePlugin:
    return _make_plugin()
