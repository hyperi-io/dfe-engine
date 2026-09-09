"""Configuration model for dfe-receiver.

Mirrors the Rust config structs in dfe-receiver/src/config/mod.rs.
All defaults match the Rust `impl Default` values exactly.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from dfe_engine.services.models.base import BaseServiceConfig
from dfe_engine.services.models.common import (
    DlqConfig,
    KafkaTlsConfig,
    SaslConfig,
)
from dfe_engine.source.models import DEFAULT_LANDING_LABEL

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

ReceiverMatchMode = Literal["key_present", "key_value_set", "key_value_use"]
"""The receiver hot-path router's whole mode set (dfe-receiver ``src/config/mod.rs``)."""


class SourceRule(BaseModel):
    """One `_source` stamping rule - the dfe-receiver ``SourceRule`` serde contract.

    Field names and semantics mirror dfe-receiver ``src/config/mod.rs`` EXACTLY
    (first match wins; only evaluated when the common header is on):

    - ``key_present``:   field exists            -> ``_source = source``
    - ``key_value_set``: field value==match_value -> ``_source = source``
    - ``key_value_use``: field exists            -> ``_source = <field value>``
    """

    model_config = ConfigDict(extra="forbid")

    field: str = Field(..., description="JSON field path (dot notation for nested)")
    mode: ReceiverMatchMode = Field(
        ..., description="Match mode (key_present | key_value_set | key_value_use)"
    )
    match_value: str | None = Field(
        default=None, description="Value to match against (key_value_set only)"
    )
    source: str | None = Field(
        default=None,
        description="_source to stamp (key_present + key_value_set; ignored for key_value_use)",
    )


class ReceiverRoutingConfig(BaseModel):
    """Message routing configuration - the dfe-receiver ``RoutingConfig`` contract.

    The engine EMITS this shape (compiled from Source definitions); the
    receiver deserialises it verbatim. Topic = ``source_to_topic[_source]``
    else ``{_source}{topic_suffix}``.
    """

    model_config = ConfigDict(extra="forbid")

    source_rules: list[SourceRule] = Field(
        default_factory=list,
        description="_source stamping rules, first match wins (compiled from Source.match)",
    )
    default_source: str = Field(
        default=DEFAULT_LANDING_LABEL, description="_source when no rule matches"
    )
    topic_suffix: str = Field(
        default="_land", description="Suffix appended to _source to form the topic"
    )
    source_to_topic: dict[str, str] = Field(
        default_factory=dict,
        description="Optional per-source topic overrides ({source: topic})",
    )
    legacy_compat: bool = Field(
        default=False,
        description="Receiver appends pre-2.2 key_value_use rules when true",
    )
    dlq: DlqConfig = Field(default_factory=DlqConfig)


# ---------------------------------------------------------------------------
# Destinations
# ---------------------------------------------------------------------------


BUS_DESTINATION = "kafka"
LOADER_DESTINATION = "loader"

BUILT_IN_DESTINATIONS = frozenset({BUS_DESTINATION, LOADER_DESTINATION})
"""The two destinations the receiver resolves without being given an address."""


class DestinationRule(BaseModel):
    """Destination routing rule."""

    model_config = ConfigDict(extra="forbid")

    match_field: str
    match_value: str
    destination: str


class DestinationsConfig(BaseModel):
    """Destination selection, plus the endpoint of every destination it names.

    ``kafka`` and ``loader`` are the receiver's built-in destinations. Any other
    name is a NAMED endpoint sitting beside these keys as
    ``<name>: {grpc: {endpoint: uri}}``, which is why extras are allowed here: a
    rule sends a matched record to a transform instance by name. The names the
    engine compiles are ``loader`` and ``dfe-transform-*`` instances, so none of
    them can collide with ``default`` or ``rules``.

    The nesting is the receiver's own ``DestinationSpec`` (``src/config/mod.rs``):
    one transport block per destination, ``grpc: {endpoint}`` or ``kafka: {topic}``.
    A bare ``grpc: <uri>`` is what the receiver read before v1.15.30 and it now
    refuses the whole config file, so the pod cannot start.
    """

    model_config = ConfigDict(extra="allow")

    default: str = Field(default="kafka", description="kafka, loader, or a named destination")
    rules: list[DestinationRule] = []

    @model_validator(mode="after")
    def validate_destinations(self) -> DestinationsConfig:
        named = self.__pydantic_extra__ or {}
        for name, entry in named.items():
            grpc = entry.get("grpc") if isinstance(entry, dict) else None
            if not isinstance(grpc, dict) or not grpc.get("endpoint"):
                msg = (
                    f"Named destination {name!r} must be a mapping carrying "
                    f"grpc.endpoint; the receiver refuses a bare grpc string"
                )
                raise ValueError(msg)
        # A destination the receiver cannot resolve silently drops every record
        # the rule matched, so nothing may name one that is neither built in nor
        # declared above.
        known = BUILT_IN_DESTINATIONS | set(named)
        unknown = {r.destination for r in self.rules} | {self.default}
        if unknown - known:
            msg = (
                f"Destinations not declared: {', '.join(sorted(unknown - known))}. "
                f"Known: {', '.join(sorted(known))}"
            )
            raise ValueError(msg)
        return self


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
    """Connection configuration for dfe-loader transport.

    The transports are the receiver's own (``LoaderConfig`` in its
    ``src/config/mod.rs``): ``grpc`` is the direct path this block exists for.
    """

    model_config = ConfigDict(extra="forbid")

    address: str = "dfe-loader:6000"
    transport: str = Field(default="kafka", description="kafka or grpc")
    timeout_ms: int = Field(
        default=5000,
        ge=0,
        description="Per-RPC deadline for the receiver's gRPC loader client (0 = none)",
    )
    grpc_endpoint: str | None = Field(
        default=None,
        description="gRPC endpoint URI; the receiver derives http://{address} when unset",
    )

    @field_validator("transport")
    @classmethod
    def validate_transport(cls, v: str) -> str:
        # `memory` discards every record and the receiver refuses it at startup,
        # so writing it here would produce a config that cannot boot.
        allowed = {"kafka", "grpc"}
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
