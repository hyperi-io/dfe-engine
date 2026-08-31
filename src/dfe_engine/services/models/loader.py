"""Configuration model for dfe-loader.

Mirrors the Rust config structs in dfe-loader/src/config/loader.rs.
All defaults match the Rust `impl Default` values exactly.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from dfe_engine.services.models.base import BaseServiceConfig
from dfe_engine.services.models.common import (
    KafkaTlsConfig,
    MemoryConfig,
    SaslConfig,
)

# ---------------------------------------------------------------------------
# Kafka (Loader-specific: consumer settings)
# ---------------------------------------------------------------------------


class LoaderKafkaConfig(BaseModel):
    """Kafka consumer configuration for the loader."""

    model_config = ConfigDict(extra="forbid")

    brokers: list[str] = Field(default_factory=lambda: ["localhost:9092"])
    group: str = "clickhouse-loader"
    topics: list[str] = Field(default_factory=lambda: ["events"])
    topic_regex: str | None = None
    client_id: str = "clickhouse-loader"
    sasl: SaslConfig | None = None
    tls: KafkaTlsConfig | None = None


# ---------------------------------------------------------------------------
# gRPC transport
# ---------------------------------------------------------------------------


class GrpcConfig(BaseModel):
    """gRPC transport configuration for receiving messages from dfe-receiver.

    When ``transport`` is "grpc" the loader starts a gRPC server on ``listen``
    and accepts Push RPCs from remote senders (e.g. dfe-receiver).

    Mirrors the Rust GrpcConfig in dfe-loader/src/config/kafka.rs.
    """

    model_config = ConfigDict(extra="forbid")

    listen: str | None = Field(
        default=None,
        description='Server listen address (e.g. "0.0.0.0:6000"). Required when transport is grpc',
    )
    recv_buffer_size: int = Field(
        default=10_000,
        gt=0,
        description="Messages buffered from incoming RPCs",
    )
    recv_timeout_ms: int = Field(
        default=100,
        ge=0,
        description="Receive timeout in milliseconds (0 = non-blocking)",
    )
    max_message_size: int = Field(
        default=16 * 1024 * 1024,
        gt=0,
        description="Maximum message size in bytes (both send and receive)",
    )
    compression: bool = Field(
        default=False,
        description="Enable gzip compression for gRPC messages",
    )
    default_topic: str = Field(
        default="default_land",
        description="Routing key for messages without a topic in the gRPC metadata",
    )


# ---------------------------------------------------------------------------
# ClickHouse
# ---------------------------------------------------------------------------


class ClickHouseConfig(BaseModel):
    """ClickHouse connection configuration."""

    model_config = ConfigDict(extra="forbid")

    hosts: list[str] = Field(default_factory=lambda: ["localhost:9000"])
    database: str = "default"
    username: str = "default"
    password: SecretStr = SecretStr("")
    protocol: str = Field(default="native", description="native or http")
    tables: list[str] = []
    tls: KafkaTlsConfig | None = None

    @field_validator("protocol")
    @classmethod
    def validate_protocol(cls, v: str) -> str:
        allowed = {"native", "http"}
        if v.lower() not in allowed:
            msg = f"Invalid protocol: {v}. Allowed: {', '.join(sorted(allowed))}"
            raise ValueError(msg)
        return v


# ---------------------------------------------------------------------------
# Payload
# ---------------------------------------------------------------------------


class PayloadConfig(BaseModel):
    """Payload format detection configuration."""

    model_config = ConfigDict(extra="forbid")

    format: str = Field(default="auto", description="auto, json, or messagepack/msgpack")
    mismatch_threshold: int = Field(default=10, ge=0)


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


class LoaderDlqConfig(BaseModel):
    """DLQ configuration for the loader."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    topic_suffix: str = ".dlq"


class LoaderRoutingConfig(BaseModel):
    """Database/table routing configuration for the loader.

    Field-for-field the loader's ``RoutingConfig``
    (dfe-loader/src/config/pipeline.rs), DEFAULTS INCLUDED. That struct is the
    contract, and serde drops an unknown key without a word, so a key invented
    here does not fail the loader -- it silently never takes effect and the
    loader falls back to its own default, sending every message somewhere the
    author never asked for. The Rust struct is the SSoT; anything added here
    must exist there first.

    Routing resolves in order: a ``rules`` CEL match wins outright, otherwise
    the table comes from the first ``table_fields`` hit (mapped through
    ``source_to_table`` when it has an entry) and the database from
    ``db_fields`` / ``org_routes``, falling back to ``default_table`` and
    ``default_db``.
    """

    model_config = ConfigDict(extra="forbid")

    rules: list[dict[str, Any]] = Field(
        default_factory=list,
        description="CEL rules {when, target, db?}, top to bottom, first match wins",
    )
    db_fields: list[str] = []
    table_fields: list[str] = Field(default_factory=lambda: ["_source"])
    default_db: str = "dfe"
    default_table: str = "default"
    org_id_field: str | None = "org_id"
    org_routes: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Per-org database routing {org_id, database?}; unlisted orgs use default_db",
    )
    source_to_table: dict[str, str] = Field(default_factory=dict)
    mapping_file: str | None = None
    topic_suffixes: list[str] = Field(default_factory=lambda: ["_land", "_load"])
    compat_v2_source: bool = Field(
        default=False,
        description="Prepend the pre-2.2 event_category fields to source_fields/table_fields",
    )
    dlq: LoaderDlqConfig = Field(default_factory=LoaderDlqConfig)


# ---------------------------------------------------------------------------
# Buffer
# ---------------------------------------------------------------------------


class LoaderBufferConfig(BaseModel):
    """Buffer flush configuration for the loader."""

    model_config = ConfigDict(extra="forbid")

    flush_bytes: int = Field(default=1_048_576, gt=0, description="1MB default")
    flush_rows: int = Field(default=10_000, gt=0)
    flush_age_secs: int = Field(default=5, gt=0)


# ---------------------------------------------------------------------------
# Timestamp Data Quality
# ---------------------------------------------------------------------------


class TimestampDqConfig(BaseModel):
    """Timestamp data quality configuration."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    max_future_seconds: int = Field(default=600, ge=0)
    max_past_seconds: int = Field(default=0, ge=0, description="0 = no limit")
    invalid_action: str = "replace_with_now"
    correct_known_bad: bool = True

    @field_validator("invalid_action")
    @classmethod
    def validate_invalid_action(cls, v: str) -> str:
        allowed = {"replace_with_now", "drop", "dlq"}
        if v.lower() not in allowed:
            msg = f"Invalid action: {v}. Allowed: {', '.join(sorted(allowed))}"
            raise ValueError(msg)
        return v


# ---------------------------------------------------------------------------
# Field Sanitization
# ---------------------------------------------------------------------------


class FieldSanitizationConfig(BaseModel):
    """Field name sanitization configuration."""

    model_config = ConfigDict(extra="forbid")

    strip_at_prefix: bool = True
    handle_numeric_prefix: bool = True
    numeric_prefix: str = "col_"
    collapse_underscores: bool = True
    trim_underscores: bool = True
    collision_strategy: str = "last_wins"


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------


class MetadataConfig(BaseModel):
    """Metadata injection and field capture configuration."""

    model_config = ConfigDict(extra="forbid")

    inject_timestamp_load: bool = True
    extract_timestamp_collector: bool = True
    collector_timestamp_path: str = "tags.collector.timestamp"

    # Tags handling
    tags_fields: list[str] = Field(
        default_factory=lambda: ["tags", "_tags", "meta", "metadata.tags"]
    )
    tags_output: str = "_tags"
    drop_tags: bool = False

    # _json capture
    capture_json: bool = True
    json_output: str = "_json"

    # _raw capture
    capture_raw: bool = True
    raw_source_fields: list[str] = Field(default_factory=lambda: ["logoriginal"])
    raw_output: str = "_raw"

    # Per-table overrides
    disable_json_tables: list[str] = []
    disable_raw_tables: list[str] = []

    # Routing field removal
    remove_routing_fields: bool = True


# ---------------------------------------------------------------------------
# Type Coercion
# ---------------------------------------------------------------------------


class CoercionConfig(BaseModel):
    """Type coercion configuration for JSON-to-ClickHouse mapping."""

    model_config = ConfigDict(extra="forbid")

    type_mappings: dict[str, str] = Field(default_factory=dict)
    unknown_type_fallback: str = "String"
    null_handling: str = Field(default="default", description="default, error, passthrough")
    null_strings: list[str] = Field(
        default_factory=lambda: [
            "null",
            "NULL",
            "Null",
            "None",
            "nil",
            "undefined",
            "\\N",
            "<null>",
            "NA",
            "N/A",
            "n/a",
            "NaN",
        ]
    )
    default_timezone: str = "UTC"
    timezone_fields: list[str] = Field(default_factory=lambda: ["tags_collector_timezone"])
    array_to_json: bool = True
    strict: bool = False

    @field_validator("null_handling")
    @classmethod
    def validate_null_handling(cls, v: str) -> str:
        allowed = {"default", "error", "passthrough"}
        if v.lower() not in allowed:
            msg = f"Invalid null_handling: {v}. Allowed: {', '.join(sorted(allowed))}"
            raise ValueError(msg)
        return v


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


class SchemaConfig(BaseModel):
    """Schema cache configuration."""

    model_config = ConfigDict(extra="forbid")

    cache_ttl_secs: int = Field(default=300, ge=0)
    refresh_on_error: bool = True


# ---------------------------------------------------------------------------
# Auto-Initialization
# ---------------------------------------------------------------------------


class AutoInitConfig(BaseModel):
    """Auto-initialization of infrastructure on startup."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    create_topics: bool = True
    topic_partitions: int = Field(default=3, ge=1)
    topic_replication_factor: int = Field(default=1, ge=1)
    create_database: bool = True
    create_table: bool = True
    create_text_index: bool = True


# ---------------------------------------------------------------------------
# Top-level config
# ---------------------------------------------------------------------------


class LoaderConfig(BaseServiceConfig):
    """Complete configuration for dfe-loader.

    Mirrors the Rust Config struct in dfe-loader/src/config/loader.rs.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    transport: str = Field(
        default="kafka",
        description="Transport backend: kafka or grpc. Bound at startup (restart required)",
    )
    kafka: LoaderKafkaConfig = Field(default_factory=LoaderKafkaConfig)
    grpc: GrpcConfig = Field(default_factory=GrpcConfig)
    clickhouse: ClickHouseConfig = Field(default_factory=ClickHouseConfig)
    payload: PayloadConfig = Field(default_factory=PayloadConfig)
    routing: LoaderRoutingConfig = Field(default_factory=LoaderRoutingConfig)
    buffer: LoaderBufferConfig = Field(default_factory=LoaderBufferConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    timestamp_dq: TimestampDqConfig = Field(default_factory=TimestampDqConfig)
    field_sanitization: FieldSanitizationConfig = Field(default_factory=FieldSanitizationConfig)
    metadata: MetadataConfig = Field(default_factory=MetadataConfig)
    coercion: CoercionConfig = Field(default_factory=CoercionConfig)
    schema_cache: SchemaConfig = Field(
        default_factory=SchemaConfig,
        alias="schema",
        description="Schema cache config (aliased from 'schema' in YAML)",
    )
    auto_init: AutoInitConfig = Field(default_factory=AutoInitConfig)

    @field_validator("transport")
    @classmethod
    def validate_transport(cls, v: str) -> str:
        allowed = {"kafka", "grpc"}
        if v.lower() not in allowed:
            msg = f"Invalid transport: {v}. Allowed: {', '.join(sorted(allowed))}"
            raise ValueError(msg)
        # Normalise, do not just accept: the loader dispatches on an EXACT match
        # (dfe-loader/src/kafka/transport.rs:546, `config.transport == "grpc"`),
        # so authoring "GRPC" would silently run the Kafka path instead.
        return v.lower()
