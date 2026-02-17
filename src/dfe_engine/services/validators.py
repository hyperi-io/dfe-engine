"""Cross-field validation for DFE service configurations.

Mirrors the Rust `validate()` methods to catch configuration errors
before they reach the services.
"""

from __future__ import annotations

from pydantic import BaseModel

from dfe_engine.services.models import (
    SERVICE_CONFIG_CLASSES,
    VALID_SERVICES,
    ArchiverConfig,
    LoaderConfig,
    ReceiverConfig,
)


class ValidationResult(BaseModel):
    """Result of configuration validation."""

    valid: bool
    errors: list[str] = []
    warnings: list[str] = []


def validate_config(service: str, config_data: dict) -> ValidationResult:
    """Validate a service configuration.

    Performs both Pydantic model validation (types, constraints) and
    cross-field business logic validation (matching the Rust validate() methods).

    Args:
        service: Service name ('receiver', 'loader', 'archiver')
        config_data: Configuration dictionary

    Returns:
        ValidationResult with errors and warnings
    """
    if service not in VALID_SERVICES:
        return ValidationResult(
            valid=False,
            errors=[f"Unknown service: {service}. Valid: {', '.join(sorted(VALID_SERVICES))}"],
        )

    config_cls = SERVICE_CONFIG_CLASSES[service]
    errors: list[str] = []
    warnings: list[str] = []

    # Phase 1: Pydantic model validation
    try:
        config = config_cls.model_validate(config_data)
    except Exception as e:
        return ValidationResult(valid=False, errors=[f"Schema validation failed: {e}"])

    # Phase 2: Cross-field validation
    if service == "receiver":
        _validate_receiver(config, errors, warnings)
    elif service == "loader":
        _validate_loader(config, errors, warnings)
    elif service == "archiver":
        _validate_archiver(config, errors, warnings)

    return ValidationResult(valid=len(errors) == 0, errors=errors, warnings=warnings)


def _validate_receiver(config: ReceiverConfig, errors: list[str], warnings: list[str]) -> None:
    """Cross-field validation for dfe-receiver config.

    Mirrors dfe-receiver/src/config/mod.rs Config::validate().
    """
    # Kafka brokers required when destination is kafka
    if config.destinations.default == "kafka" and not config.kafka.brokers:
        errors.append("kafka.brokers is required when destinations.default is 'kafka'")

    # Auth mode consistency
    if config.server.auth.mode == "bearer" and (
        not config.server.auth.bearer.tokens
        and not config.server.auth.bearer.secret_source
    ):
        warnings.append(
            "auth.mode is 'bearer' but no tokens or secret_source configured"
        )

    if config.server.auth.mode == "mtls" and not config.server.tls.enabled:
        errors.append("auth.mode is 'mtls' but TLS is not enabled")

    if config.server.auth.mode == "both" and not config.server.tls.enabled:
        errors.append("auth.mode is 'both' but TLS is not enabled")

    # TLS requires cert + key
    if config.server.tls.enabled:
        has_file = config.server.tls.cert_file and config.server.tls.key_file
        has_secret = config.server.tls.cert_secret and config.server.tls.key_secret
        if not has_file and not has_secret:
            errors.append(
                "TLS enabled but neither cert_file/key_file nor cert_secret/key_secret configured"
            )

    # gRPC bind address conflict
    if config.grpc.enabled and config.grpc.bind_address == config.server.bind_address:
        errors.append("gRPC and HTTP server cannot share the same bind_address")


def _validate_loader(config: LoaderConfig, errors: list[str], warnings: list[str]) -> None:
    """Cross-field validation for dfe-loader config.

    Mirrors dfe-loader/src/config/loader.rs Config::validate().
    """
    # Kafka validation
    if not config.kafka.brokers:
        errors.append("At least one Kafka broker must be configured")

    if not config.kafka.topics and not config.kafka.topic_regex:
        errors.append("Either kafka.topics or kafka.topic_regex must be configured")

    # ClickHouse validation
    if not config.clickhouse.hosts:
        errors.append("At least one ClickHouse host must be configured")

    # Buffer validation (already enforced by Pydantic gt=0, but explicit for clarity)
    if config.buffer.flush_bytes == 0:
        errors.append("buffer.flush_bytes must be greater than 0")

    if config.buffer.flush_rows == 0:
        errors.append("buffer.flush_rows must be greater than 0")

    # SASL validation
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

    # Routing warnings
    if config.routing.route_all_by_org and config.routing.routed_orgs:
        warnings.append(
            "route_all_by_org is true, routed_orgs list will be ignored"
        )


def _validate_archiver(config: ArchiverConfig, errors: list[str], warnings: list[str]) -> None:
    """Cross-field validation for dfe-archiver config."""
    # Kafka validation
    if not config.kafka.brokers:
        errors.append("At least one Kafka broker must be configured")

    if not config.kafka.topics:
        warnings.append("No Kafka topics configured")

    # Destination URL validation
    dest = config.archive.destination
    valid_schemes = ("file://", "s3://", "gs://", "az://", "minio://")
    if not any(dest.startswith(s) for s in valid_schemes):
        errors.append(
            f"Invalid archive.destination scheme: {dest}. "
            f"Must start with one of: {', '.join(valid_schemes)}"
        )

    # Backend-specific validation
    if dest.startswith("s3://") and not config.archive.s3:
        warnings.append("archive.destination is S3 but archive.s3 config not provided")

    if dest.startswith("gs://") and not config.archive.gcs:
        warnings.append("archive.destination is GCS but archive.gcs config not provided")

    if dest.startswith("az://") and not config.archive.azure:
        warnings.append("archive.destination is Azure but archive.azure config not provided")

    if dest.startswith("minio://") and not config.archive.minio:
        warnings.append("archive.destination is MinIO but archive.minio config not provided")

    # SASL consistency
    if config.kafka.security_protocol.upper() in ("SASL_PLAINTEXT", "SASL_SSL"):
        if not config.kafka.sasl_mechanism:
            errors.append(
                f"security_protocol is {config.kafka.security_protocol} "
                "but sasl_mechanism is not set"
            )

    # Expression routing requires fields
    if config.routing.mode == "expression" and not config.routing.expression_fields:
        errors.append(
            "routing.mode is 'expression' but no expression_fields configured"
        )
