"""Built-in plugin for dfe-transform-wasm.

Consolidates descriptor, validation, sizing, and template overrides
for the WASM-based transform service. Type 2 service (multi-source)
with flat source list (no config tree — tree is vector-only).
"""

from __future__ import annotations

from typing import Any

from dfe_engine.services.descriptor import KafkaRole, ServiceDescriptor
from dfe_engine.services.models.common import production_sasl_scram
from dfe_engine.services.plugin import ServicePlugin

descriptor = ServiceDescriptor(
    name="transform-wasm",
    display_name="DFE Transform (WASM)",
    image="harbor.hyperi.io/dfe/dfe-transform-wasm",
    default_port=8080,
    metrics_port=9090,
    kafka_role=KafkaRole.BOTH,
    consumer_group="dfe-transform-wasm",
    liveness_paths=("/health/live",),
    readiness_paths=("/health/ready",),
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
        "memory": {"max_memory_mb": 2048},
    },
    "large": {
        "memory": {"max_memory_mb": 4096},
    },
    "xlarge": {
        "memory": {"max_memory_mb": 8192},
    },
}

_keda_defaults: dict[str, Any] = {
    "min_replicas": 1,
    "max_replicas": 8,
    "kafka_trigger": {
        "consumer_group": "dfe-transform-wasm",
        "lag_threshold": 500,
    },
}

_template_overrides: dict[str, dict[str, Any]] = {
    "production": {
        "kafka": {
            "consumer": {
                "sasl": production_sasl_scram(),
                "tls": {"enabled": True},
            },
            "producer": {
                "sasl": production_sasl_scram(),
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
