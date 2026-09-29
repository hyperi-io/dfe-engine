"""Built-in plugin for dfe-transform-wasm.

Consolidates descriptor, validation, sizing, and template overrides
for the WASM-based transform service. Type 2 service (multi-source)
with flat source list (no config tree -- tree is vector-only).
"""

from typing import Any

from dfe_engine.services.descriptor import KafkaRole, ServiceDescriptor
from dfe_engine.services.plugin import ServicePlugin

descriptor = ServiceDescriptor(
    name="transform-wasm",
    display_name="DFE Transform (WASM)",
    image="ghcr.io/hyperi-io/dfe-transform-wasm",
    default_port=8080,
    metrics_port=9090,
    kafka_role=KafkaRole.BOTH,
    consumer_group="dfe-transform-wasm",
    liveness_paths=("/livez",),
    readiness_paths=("/readyz",),
    description="Kafka-to-Kafka transform using WASM modules. Multi-source, flat list.",
)


def _validate_transform_wasm(config: Any, errors: list[str], warnings: list[str]) -> None:
    """Cross-field validation for dfe-transform-wasm config."""
    if not config.kafka.consumer.brokers and not config.ipc.enabled:
        errors.append("kafka.consumer.brokers is required when IPC is not enabled")

    if not config.kafka.producer.brokers and not config.ipc.enabled:
        errors.append("kafka.producer.brokers is required when IPC is not enabled")

    # Check source names are unique
    source_names = [s.name for s in config.sources]
    if len(source_names) != len(set(source_names)):
        errors.append("Source names must be unique within a deployment")

    # Validate each source has a wasm_module
    for src in config.sources:
        if not src.wasm_module:
            errors.append(f"Source '{src.name}' is missing wasm_module path")


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
        # Empty: every deployment names its own brokers.
        "kafka": {"consumer": {"brokers": []}, "producer": {"brokers": []}},
        "metrics": {"address": "0.0.0.0:9090"},
    },
}


def _make_plugin() -> ServicePlugin:
    from dfe_engine.deployment.models.transform_wasm import (
        TransformWasmDeploymentConfig,
    )
    from dfe_engine.services.models.transform_wasm import TransformWasmConfig

    return ServicePlugin(
        descriptor=descriptor,
        config_class=TransformWasmConfig,
        deployment_class=TransformWasmDeploymentConfig,
        validate_config=_validate_transform_wasm,
        sizing_overrides=_sizing_overrides,
        keda_defaults=_keda_defaults,
        default_size="medium",
        config_template_overrides=_template_overrides,
    )


def plugin() -> ServicePlugin:
    return _make_plugin()
