"""Shared configuration models used across DFE Rust services."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


class SaslConfig(BaseModel):
    """SASL authentication for Kafka connections.

    Supports PLAIN, SCRAM-SHA-256, SCRAM-SHA-512, OAUTHBEARER, and AWS MSK IAM.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    mechanism: str = Field(default="scram_sha_512", description="SASL mechanism")
    username: str = ""
    password: SecretStr = SecretStr("")

    # OAuth 2.0 / OIDC (OAUTHBEARER mechanism)
    oauth_token_endpoint: str | None = None
    oauth_client_id: str | None = None
    oauth_client_secret: SecretStr | None = None
    oauth_scope: str | None = None
    oauth_extensions: str | None = None

    # AWS MSK IAM
    aws_region: str | None = None
    aws_access_key_id: SecretStr | None = None
    aws_secret_access_key: SecretStr | None = None
    aws_session_token: SecretStr | None = None
    aws_profile: str | None = None

    @field_validator("mechanism")
    @classmethod
    def validate_mechanism(cls, v: str) -> str:
        allowed = {
            "none",
            "plain",
            "scram_sha_256",
            "scram_sha_512",
            "oauthbearer",
            "aws_msk_iam",
        }
        normalized = v.lower().replace("-", "_")
        if normalized not in allowed:
            msg = f"Unknown SASL mechanism: {v}. Allowed: {', '.join(sorted(allowed))}"
            raise ValueError(msg)
        return v


def production_sasl_scram(**overrides: Any) -> dict[str, Any]:
    """The canonical production Kafka SASL block (SCRAM-SHA-512) as a plain dict.

    DFE-owned brokers run SASL/SCRAM-SHA-512 everywhere (see the Kafka SCRAM
    standard). This is the ONE source for the production SASL block that the
    per-service config templates + seeds emit, built FROM :class:`SaslConfig` so
    the full key set can never drift from the schema again (a hand-typed copy in
    transform_vrl had silently dropped 10 keys). Values are the production
    defaults: enabled, scram_sha_512, empty username/password (the deploy fills
    them from the secret store). Pass ``overrides`` for a rare per-service tweak.

    The SecretStr fields are revealed to their plain (empty) values so the emitted
    template carries ``""`` - never the masked ``"**********"`` a json dump gives.
    """
    cfg = SaslConfig(enabled=True, mechanism="scram_sha_512", **overrides)
    data = cfg.model_dump()
    for key, value in list(data.items()):
        if isinstance(value, SecretStr):
            data[key] = value.get_secret_value()
    return data


class KafkaTlsConfig(BaseModel):
    """TLS configuration for Kafka connections.

    Used by loader (ca_cert_file in YAML) and receiver (ca_file in YAML).
    Accepts both field names via alias + populate_by_name.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    enabled: bool = False
    ca_file: str | None = Field(default=None, alias="ca_cert_file")
    cert_file: str | None = None
    key_file: str | None = None
    skip_verify: bool = False


class MetricsConfig(BaseModel):
    """Prometheus metrics server configuration."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    address: str = "0.0.0.0:9090"
    path: str = "/metrics"


class MemoryConfig(BaseModel):
    """Memory limits and backpressure configuration."""

    model_config = ConfigDict(extra="forbid")

    limit_bytes: int = Field(default=0, ge=0, description="0 = auto-detect (67% of available)")
    pressure_threshold: float = Field(default=0.8, ge=0.0, le=1.0)
    tracking_enabled: bool = True


class DlqConfig(BaseModel):
    """Dead Letter Queue configuration."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    topic: str = "dlq_land"


class LoggingConfig(BaseModel):
    """Logging configuration."""

    model_config = ConfigDict(extra="forbid")

    level: str = "info"
    format: str = "json"

    @field_validator("level")
    @classmethod
    def validate_level(cls, v: str) -> str:
        allowed = {"trace", "debug", "info", "warn", "error"}
        if v.lower() not in allowed:
            msg = f"Invalid log level: {v}. Allowed: {', '.join(sorted(allowed))}"
            raise ValueError(msg)
        return v

    @field_validator("format")
    @classmethod
    def validate_format(cls, v: str) -> str:
        allowed = {"json", "text"}
        if v.lower() not in allowed:
            msg = f"Invalid log format: {v}. Allowed: {', '.join(sorted(allowed))}"
            raise ValueError(msg)
        return v
