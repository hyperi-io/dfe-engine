"""Built-in plugin for dfe-archiver.

Consolidates archiver-specific descriptor, validation, and template overrides.
"""

from typing import Any

from dfe_engine.services.descriptor import ServiceDescriptor
from dfe_engine.services.plugin import ServicePlugin

descriptor = ServiceDescriptor(
    name="archiver",
    display_name="DFE Archiver",
    description="Kafka consumer -- archives events to S3/file storage.",
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

    # Unset is the app's own per-transport default. Only an explicit empty list is refused.
    if config.routing.mode == "expression" and config.routing.expression_fields == []:
        errors.append("routing.mode is 'expression' but no expression_fields configured")


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
        # Empty: every deployment names its own brokers.
        "kafka": {"brokers": []},
        "metrics": {"address": "0.0.0.0:9090"},
        "memory": {"limit_bytes": 0},
    },
}


def _make_plugin() -> ServicePlugin:
    from dfe_engine.services.models.archiver import ArchiverConfig

    return ServicePlugin(
        descriptor=descriptor,
        config_class=ArchiverConfig,
        validate_config=_validate_archiver,
        config_template_overrides=_template_overrides,
    )


def plugin() -> ServicePlugin:
    """The archiver plugin, loaded through the ``dfe_engine.services`` entry point."""
    return _make_plugin()
