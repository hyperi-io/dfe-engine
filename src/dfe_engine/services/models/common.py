"""Shared configuration models used across DFE Rust services."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator


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

    @model_validator(mode="before")
    @classmethod
    def _coerce_legacy_max_memory_mb(cls, data: object) -> object:
        """Accept deprecated ``max_memory_mb`` from older seeded defaults (MB → bytes)."""
        if not isinstance(data, dict):
            return data
        if "limit_bytes" in data or "max_memory_mb" not in data:
            return data
        mb = data.pop("max_memory_mb")
        if mb == 0:
            data.setdefault("limit_bytes", 0)
        else:
            data["limit_bytes"] = int(mb) * 1024 * 1024
        return data


class DlqConfig(BaseModel):
    """Dead Letter Queue configuration for the receiver's routing block."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    # The per-app topic dfe-schemas declares and the engine creates at bootstrap.
    topic: str = "dfe_receiver_dlq"


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
