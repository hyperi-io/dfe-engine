"""Built-in plugin for dfe-transform-vrl.

Consolidates descriptor, validation, and template overrides
for the embedded VRL transform service. Unlike transform-vector, this
service embeds the VRL crate directly -- no subprocess, no Vector binary.
Simpler config: flat source/sink Kafka, VRL transforms directory.
"""

from typing import Any

from dfe_engine.services.descriptor import KafkaRole, ServiceDescriptor
from dfe_engine.services.plugin import ServicePlugin

descriptor = ServiceDescriptor(
    name="transform-vrl",
    display_name="DFE Transform (VRL)",
    image="ghcr.io/hyperi-io/dfe-transform-vrl",
    default_port=9000,
    metrics_port=9090,
    kafka_role=KafkaRole.BOTH,
    consumer_group="dfe-transform-vrl",
    liveness_paths=("/livez",),
    readiness_paths=("/readyz",),
    description=(
        "Kafka-to-Kafka transform using embedded VRL engine. "
        "Single pipeline with wrapper-controlled source/sink."
    ),
    extra_ports={"health": 9000},
)


def _validate_transform_vrl(config: Any, errors: list[str], warnings: list[str]) -> None:
    """Cross-field validation for dfe-transform-vrl config."""
    if not config.source.brokers:
        errors.append("source.brokers is required")

    if not config.sink.topic:
        errors.append("sink.topic is required")

    if not config.source.topics:
        errors.append("source.topics is required (at least one topic)")

    if config.transforms.dir is None and config.transforms.files is None:
        errors.append(
            "transforms.dir or transforms.files is required (at least one VRL transform source)"
        )


_template_overrides: dict[str, dict[str, Any]] = {
    "production": {
        "source": {
            "sasl": {
                "enabled": True,
                "mechanism": "scram_sha_512",
                "username": "",
                "password": "",
            },
            "tls": {"enabled": True},
        },
        "sink": {
            "sasl": {
                "enabled": True,
                "mechanism": "scram_sha_512",
                "username": "",
                "password": "",
            },
            "tls": {"enabled": True},
        },
    },
    "k8s": {
        # Empty: every deployment names its own brokers.
        "source": {"brokers": []},
        "sink": {"brokers": []},
        "health": {"address": "0.0.0.0:9000"},
        "metrics": {"address": "0.0.0.0:9090"},
    },
}


def _make_plugin() -> ServicePlugin:
    from dfe_engine.services.models.transform_vrl import TransformVrlConfig

    return ServicePlugin(
        descriptor=descriptor,
        config_class=TransformVrlConfig,
        validate_config=_validate_transform_vrl,
        config_template_overrides=_template_overrides,
    )


def plugin() -> ServicePlugin:
    return _make_plugin()
