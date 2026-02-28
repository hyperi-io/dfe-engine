"""Configuration model for dfe-receiver.

Mirrors the Rust config structs in dfe-receiver/src/config/mod.rs.
All defaults match the Rust `impl Default` values exactly.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from dfe_engine.services.models.base import BaseServiceConfig
from dfe_engine.services.models.common import (
    DlqConfig,
    KafkaTlsConfig,
    SaslConfig,
)


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------


class TlsConfig(BaseModel):
    """TLS/mTLS configuration for the HTTP server."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    cert_file: str | None = None
    key_file: str | None = None
    ca_file: str | None = None
    client_auth: str = Field(default="none", description="none, optional, required")
    cert_secret: str | None = None
    key_secret: str | None = None
    ca_secret: str | None = None
    refresh_interval_secs: int = Field(default=3600, ge=0)

    @field_validator("client_auth")
    @classmethod
    def validate_client_auth(cls, v: str) -> str:
        allowed = {"none", "optional", "required"}
        if v.lower() not in allowed:
            msg = f"Invalid client_auth: {v}. Allowed: {', '.join(sorted(allowed))}"
            raise ValueError(msg)
        return v


class AcceptedHeader(BaseModel):
    """An accepted authentication header definition."""

    model_config = ConfigDict(extra="forbid")

    name: str
    values: list[str] = []


class BearerConfig(BaseModel):
    """Bearer token authentication configuration."""

    model_config = ConfigDict(extra="forbid")

    tokens: list[SecretStr] = []
    secret_source: str | None = None
    refresh_interval_secs: int = Field(default=300, ge=0)


class AuthConfig(BaseModel):
    """Authentication configuration for the HTTP server."""

    model_config = ConfigDict(extra="forbid")

    mode: str = Field(default="none", description="none, header, bearer, mtls, both")
    accepted_headers: list[AcceptedHeader] = Field(
        default_factory=lambda: [AcceptedHeader(name="x-hyperi-agent", values=["1.0"])]
    )
    bearer: BearerConfig = Field(default_factory=BearerConfig)
    header_name: str = ""
    header_values: list[str] = []

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, v: str) -> str:
        allowed = {"none", "header", "bearer", "mtls", "both"}
        if v.lower() not in allowed:
            msg = f"Invalid auth mode: {v}. Allowed: {', '.join(sorted(allowed))}"
            raise ValueError(msg)
        return v


class ServerConfig(BaseModel):
    """HTTP server configuration."""

    model_config = ConfigDict(extra="forbid")

    bind_address: str = "0.0.0.0:8080"
    max_body_size: int = Field(default=10 * 1024 * 1024, ge=0, description="bytes")
    request_timeout_ms: int = Field(default=30_000, ge=0)
    tls: TlsConfig = Field(default_factory=TlsConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)


# ---------------------------------------------------------------------------
# gRPC
# ---------------------------------------------------------------------------


class GrpcConfig(BaseModel):
    """gRPC server configuration."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    bind_address: str = "0.0.0.0:6000"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class ValidationConfig(BaseModel):
    """Input validation configuration."""

    model_config = ConfigDict(extra="forbid")

    require_json: bool = True
    required_fields: list[str] = []
    dlq_on_invalid: bool = True


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


class ReceiverRoutingConfig(BaseModel):
    """Message routing configuration for the receiver."""

    model_config = ConfigDict(extra="forbid")

    topic_fields: list[str] = Field(
        default_factory=lambda: ["tags.event.category", "event_category"]
    )
    default_topic: str = "unmatched"
    topic_suffix: str = "_land"
    category_to_topic: dict[str, str] = Field(default_factory=dict)
    dlq: DlqConfig = Field(default_factory=DlqConfig)


# ---------------------------------------------------------------------------
# Destinations
# ---------------------------------------------------------------------------


class DestinationRule(BaseModel):
    """Destination routing rule."""

    model_config = ConfigDict(extra="forbid")

    match_field: str
    match_value: str
    destination: str


class DestinationsConfig(BaseModel):
    """Destination selection configuration."""

    model_config = ConfigDict(extra="forbid")

    default: str = Field(default="kafka", description="kafka or loader")
    rules: list[DestinationRule] = []

    @field_validator("default")
    @classmethod
    def validate_default(cls, v: str) -> str:
        allowed = {"kafka", "loader"}
        if v.lower() not in allowed:
            msg = f"Invalid default destination: {v}. Allowed: {', '.join(sorted(allowed))}"
            raise ValueError(msg)
        return v


# ---------------------------------------------------------------------------
# Kafka (Receiver-specific: producer settings)
# ---------------------------------------------------------------------------


class ProducerConfig(BaseModel):
    """Kafka producer settings."""

    model_config = ConfigDict(extra="forbid")

    batch_size: int = Field(default=8 * 1024 * 1024, gt=0, description="bytes")
    batch_messages: int = Field(default=10_000, gt=0)
    linger_ms: int = Field(default=20, ge=0)
    compression: str = "lz4"
    acks: str = Field(default="all", description="0, 1, or all")
    retries: int = Field(default=5, ge=0)

    @field_validator("compression")
    @classmethod
    def validate_compression(cls, v: str) -> str:
        allowed = {"none", "gzip", "snappy", "lz4", "zstd"}
        if v.lower() not in allowed:
            msg = f"Invalid compression: {v}. Allowed: {', '.join(sorted(allowed))}"
            raise ValueError(msg)
        return v


class ReceiverKafkaConfig(BaseModel):
    """Kafka producer configuration for the receiver."""

    model_config = ConfigDict(extra="forbid")

    brokers: list[str] = []
    client_id: str = "dfe-receiver"
    sasl: SaslConfig | None = None
    tls: KafkaTlsConfig = Field(default_factory=KafkaTlsConfig)
    producer: ProducerConfig = Field(default_factory=ProducerConfig)


# ---------------------------------------------------------------------------
# Loader connection
# ---------------------------------------------------------------------------


class LoaderConnectionConfig(BaseModel):
    """Connection configuration for dfe-loader transport."""

    model_config = ConfigDict(extra="forbid")

    address: str = "dfe-loader:9000"
    transport: str = Field(default="kafka", description="kafka, zenoh, or memory")
    timeout_ms: int = Field(default=5000, ge=0)

    @field_validator("transport")
    @classmethod
    def validate_transport(cls, v: str) -> str:
        allowed = {"kafka", "zenoh", "memory"}
        if v.lower() not in allowed:
            msg = f"Invalid transport: {v}. Allowed: {', '.join(sorted(allowed))}"
            raise ValueError(msg)
        return v


# ---------------------------------------------------------------------------
# Buffer
# ---------------------------------------------------------------------------


class ReceiverBufferConfig(BaseModel):
    """Buffer and memory configuration for the receiver."""

    model_config = ConfigDict(extra="forbid")

    memory_limit: int = Field(default=0, ge=0, description="0 = auto-detect")
    pressure_threshold: float = Field(default=0.8, ge=0.0, le=1.0)


# ---------------------------------------------------------------------------
# Top-level config
# ---------------------------------------------------------------------------


class ReceiverConfig(BaseServiceConfig):
    """Complete configuration for dfe-receiver.

    Mirrors the Rust Config struct in dfe-receiver/src/config/mod.rs.
    """

    server: ServerConfig = Field(default_factory=ServerConfig)
    grpc: GrpcConfig = Field(default_factory=GrpcConfig)
    validation: ValidationConfig = Field(default_factory=ValidationConfig)
    routing: ReceiverRoutingConfig = Field(default_factory=ReceiverRoutingConfig)
    destinations: DestinationsConfig = Field(default_factory=DestinationsConfig)
    kafka: ReceiverKafkaConfig = Field(default_factory=ReceiverKafkaConfig)
    loader: LoaderConnectionConfig = Field(default_factory=LoaderConnectionConfig)
    buffer: ReceiverBufferConfig = Field(default_factory=ReceiverBufferConfig)
