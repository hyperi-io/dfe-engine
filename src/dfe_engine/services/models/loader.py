"""Configuration model for dfe-loader.

Mirrors the Rust config structs in dfe-loader/src/config/loader.rs.
All defaults match the Rust `impl Default` values exactly.
"""

from __future__ import annotations

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

    Two modes:
    - Legacy: inspect table_fields, look up category_to_table map.
    - Source routing: the ``_source`` field directly determines the table name.
      When enabled, table_fields and category_to_table are ignored.
    """

    model_config = ConfigDict(extra="forbid")

    source_routing: bool = Field(
        default=False,
        description="Use _source field for direct table routing",
    )
    source_field: str = Field(
        default="_source",
        description="JSON field containing the source name (when source_routing=True)",
    )
    db_fields: list[str] = []
    table_fields: list[str] = Field(
        default_factory=lambda: ["event_category", "tags.event_category"]
    )
    default_db: str = "common"
    default_table: str = "common"
    org_id_field: str | None = "org_id"
    routed_orgs: list[str] = []
    route_all_by_org: bool = False
    category_to_table: dict[str, str] = Field(default_factory=dict)
    mapping_file: str | None = None
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
    timezone_fields: list[str] = Field(
        default_factory=lambda: ["tags_collector_timezone"]
    )
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

    kafka: LoaderKafkaConfig = Field(default_factory=LoaderKafkaConfig)
    clickhouse: ClickHouseConfig = Field(default_factory=ClickHouseConfig)
    payload: PayloadConfig = Field(default_factory=PayloadConfig)
    routing: LoaderRoutingConfig = Field(default_factory=LoaderRoutingConfig)
    buffer: LoaderBufferConfig = Field(default_factory=LoaderBufferConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    timestamp_dq: TimestampDqConfig = Field(default_factory=TimestampDqConfig)
    field_sanitization: FieldSanitizationConfig = Field(
        default_factory=FieldSanitizationConfig
    )
    metadata: MetadataConfig = Field(default_factory=MetadataConfig)
    coercion: CoercionConfig = Field(default_factory=CoercionConfig)
    schema_cache: SchemaConfig = Field(
        default_factory=SchemaConfig,
        alias="schema",
        description="Schema cache config (aliased from 'schema' in YAML)",
    )
    auto_init: AutoInitConfig = Field(default_factory=AutoInitConfig)
