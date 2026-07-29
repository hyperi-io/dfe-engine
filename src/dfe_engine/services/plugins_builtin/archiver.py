"""Built-in plugin for dfe-archiver.

Consolidates archiver-specific descriptor, validation, sizing, and template
overrides.
"""

from __future__ import annotations

from typing import Any

from dfe_engine.services.descriptor import KafkaRole, ServiceDescriptor
from dfe_engine.services.plugin import ServicePlugin

descriptor = ServiceDescriptor(
    name="archiver",
    display_name="DFE Archiver",
    image="harbor.hyperi.io/dfe/dfe-archiver",
    default_port=8080,
    metrics_port=9090,
    kafka_role=KafkaRole.CONSUMER,
    consumer_group="dfe-archiver",
    # Served by scalo's metrics server on metrics_port, same as every other
    # service. The old note here claimed the archiver had no health API and
    # probed /metrics as a stand-in; it does have one, and probing the scrape
    # handler meant a liveness check that passed whenever the exporter answered
    # and shipped a full metrics payload every period.
    liveness_paths=("/livez",),
    readiness_paths=("/readyz",),
    description="Kafka consumer — archives events to S3/file storage.",
)


def _validate_archiver(config: Any, errors: list[str], warnings: list[str]) -> None:
    """Cross-field validation for dfe-archiver config."""
    if not config.kafka.brokers:
        errors.append("At least one Kafka broker must be configured")

    if not config.kafka.topics:
        warnings.append("No Kafka topics configured")

    dest = config.archive.destination
    valid_schemes = ("file://", "s3://", "gs://", "az://", "minio://")
    if not any(dest.startswith(s) for s in valid_schemes):
        errors.append(
            f"Invalid archive.destination scheme: {dest}. "
            f"Must start with one of: {', '.join(valid_schemes)}"
        )

    if dest.startswith("s3://") and not config.archive.s3:
        warnings.append("archive.destination is S3 but archive.s3 config not provided")

    if dest.startswith("gs://") and not config.archive.gcs:
        warnings.append("archive.destination is GCS but archive.gcs config not provided")

    if dest.startswith("az://") and not config.archive.azure:
        warnings.append("archive.destination is Azure but archive.azure config not provided")

    if dest.startswith("minio://") and not config.archive.minio:
        warnings.append("archive.destination is MinIO but archive.minio config not provided")

    if config.kafka.security_protocol.upper() in ("SASL_PLAINTEXT", "SASL_SSL"):
        if not config.kafka.sasl_mechanism:
            errors.append(
                f"security_protocol is {config.kafka.security_protocol} "
                "but sasl_mechanism is not set"
            )

    if config.routing.mode == "expression" and not config.routing.expression_fields:
        errors.append("routing.mode is 'expression' but no expression_fields configured")


_sizing_overrides: dict[str, dict[str, Any]] = {
    "xs": {
        "buffer": {"flush_bytes": 16_777_216, "writer_parallelism": 1},
    },
    "small": {
        "buffer": {"flush_bytes": 33_554_432, "writer_parallelism": 2},
    },
    "medium": {
        "buffer": {"flush_bytes": 134_217_728, "writer_parallelism": 4},
    },
    "large": {
        "buffer": {"flush_bytes": 268_435_456, "writer_parallelism": 8},
    },
    "xlarge": {
        "buffer": {"flush_bytes": 536_870_912, "writer_parallelism": 16},
        "compression": {"level": 9},
    },
}

_keda_defaults: dict[str, Any] = {
    "min_replicas": 1,
    "max_replicas": 4,
    "kafka_trigger": {
        "consumer_group": "dfe-archiver",
        "lag_threshold": 100000,
    },
}

_template_overrides: dict[str, dict[str, Any]] = {
    "production": {
        "kafka": {
            "security_protocol": "SASL_SSL",
            "sasl_mechanism": "SCRAM-SHA-512",
        },
        "compression": {"codec": "zstd", "level": 6},
        "buffer": {"flush_bytes": 128 * 1024 * 1024, "writer_parallelism": 8},
    },
    "k8s": {
        "kafka": {"brokers": ["kafka-bootstrap.kafka.svc.cluster.local:9092"]},
        "metrics": {"address": "0.0.0.0:9090"},
        "memory": {"limit_bytes": 0},
    },
}


def _make_plugin() -> ServicePlugin:
    from dfe_engine.deployment.models.archiver import ArchiverDeploymentConfig
    from dfe_engine.services.models.archiver import ArchiverConfig

    return ServicePlugin(
        descriptor=descriptor,
        config_class=ArchiverConfig,
        deployment_class=ArchiverDeploymentConfig,
        validate_config=_validate_archiver,
        sizing_overrides=_sizing_overrides,
        keda_defaults=_keda_defaults,
        default_size="small",
        config_template_overrides=_template_overrides,
    )


def plugin() -> ServicePlugin:
    return _make_plugin()
