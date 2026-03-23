"""Configuration model for dfe-archiver.

Mirrors the Rust config structs in dfe-archiver/src/config/types.rs.
All defaults match the Rust `impl Default` values exactly.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from dfe_engine.services.models.base import BaseServiceConfig
from dfe_engine.services.models.common import MemoryConfig

# ---------------------------------------------------------------------------
# Kafka (Archiver-specific: consumer with flat SASL fields)
# ---------------------------------------------------------------------------


class ArchiverKafkaConfig(BaseModel):
    """Kafka consumer configuration for the archiver.

    Uses flat SASL fields (sasl_mechanism, sasl_username, etc.) matching
    the archiver's config structure.
    """

    model_config = ConfigDict(extra="forbid")

    brokers: list[str] = Field(default_factory=lambda: ["localhost:9092"])
    group_id: str = "dfe-archiver"
    topics: list[str] = []
    sasl_mechanism: str | None = None
    security_protocol: str = "PLAINTEXT"
    sasl_username: str | None = None
    sasl_password: SecretStr | None = None
    batch_size: int = Field(default=10_000, gt=0)
    max_poll_interval_ms: int = Field(default=300_000, gt=0)
    session_timeout_ms: int = Field(default=30_000, gt=0)

    @field_validator("security_protocol")
    @classmethod
    def validate_security_protocol(cls, v: str) -> str:
        allowed = {"PLAINTEXT", "SASL_PLAINTEXT", "SSL", "SASL_SSL"}
        if v.upper() not in allowed:
            msg = f"Invalid security_protocol: {v}. Allowed: {', '.join(sorted(allowed))}"
            raise ValueError(msg)
        return v


# ---------------------------------------------------------------------------
# Archive destination
# ---------------------------------------------------------------------------


class S3Config(BaseModel):
    """S3 storage backend configuration."""

    model_config = ConfigDict(extra="forbid")

    region: str | None = None
    endpoint: str | None = None
    access_key_id: SecretStr | None = None
    secret_access_key: SecretStr | None = None
    bucket: str = ""


class GcsConfig(BaseModel):
    """Google Cloud Storage backend configuration."""

    model_config = ConfigDict(extra="forbid")

    project_id: str | None = None
    service_account_key: SecretStr | None = None
    bucket: str = ""


class AzureConfig(BaseModel):
    """Azure Blob Storage backend configuration."""

    model_config = ConfigDict(extra="forbid")

    account_name: str = ""
    account_key: SecretStr | None = None
    container: str = ""


class MinioConfig(BaseModel):
    """MinIO (S3-compatible) storage backend configuration."""

    model_config = ConfigDict(extra="forbid")

    endpoint: str = ""
    access_key: SecretStr = SecretStr("")
    secret_key: SecretStr = SecretStr("")
    bucket: str = ""
    use_ssl: bool = False


class ArchiveConfig(BaseModel):
    """Archive output configuration."""

    model_config = ConfigDict(extra="forbid")

    destination: str = "file:///var/data/archive"
    path_template: str = "{topic}/{year}/{month}/{day}/{hour}"
    file_extension: str = "jsonl"
    roll_size_bytes: int = Field(
        default=1024 * 1024 * 1024, gt=0, description="1GB final compressed file size"
    )
    roll_interval_secs: int = Field(default=3600, gt=0)
    s3: S3Config | None = None
    gcs: GcsConfig | None = None
    azure: AzureConfig | None = None
    minio: MinioConfig | None = None


# ---------------------------------------------------------------------------
# Buffer
# ---------------------------------------------------------------------------


class ArchiverBufferConfig(BaseModel):
    """Buffer management configuration for the archiver."""

    model_config = ConfigDict(extra="forbid")

    flush_bytes: int = Field(default=64 * 1024 * 1024, gt=0, description="64MB default")
    flush_age_secs: int = Field(default=60, gt=0)
    flush_records: int = Field(default=100_000, gt=0)
    writer_parallelism: int = Field(default=4, ge=1)


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


class ArchiverRoutingConfig(BaseModel):
    """Message routing configuration for the archiver."""

    model_config = ConfigDict(extra="forbid")

    mode: str = Field(default="topic", description="topic or expression")
    expression_fields: list[str] = []
    default_segment: str = "unknown"

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, v: str) -> str:
        allowed = {"topic", "expression"}
        if v.lower() not in allowed:
            msg = f"Invalid routing mode: {v}. Allowed: {', '.join(sorted(allowed))}"
            raise ValueError(msg)
        return v


# ---------------------------------------------------------------------------
# Compression
# ---------------------------------------------------------------------------


class CompressionConfig(BaseModel):
    """Compression configuration for archive files."""

    model_config = ConfigDict(extra="forbid")

    codec: str = "zstd"
    level: int = Field(default=3, ge=0)
    enabled: bool = True

    @field_validator("codec")
    @classmethod
    def validate_codec(cls, v: str) -> str:
        allowed = {"none", "zstd", "lz4", "snappy", "gzip"}
        if v.lower() not in allowed:
            msg = f"Invalid codec: {v}. Allowed: {', '.join(sorted(allowed))}"
            raise ValueError(msg)
        return v


# ---------------------------------------------------------------------------
# Top-level config
# ---------------------------------------------------------------------------


class ArchiverConfig(BaseServiceConfig):
    """Complete configuration for dfe-archiver.

    Mirrors the Rust Config struct in dfe-archiver/src/config/types.rs.
    """

    kafka: ArchiverKafkaConfig = Field(default_factory=ArchiverKafkaConfig)
    archive: ArchiveConfig = Field(default_factory=ArchiveConfig)
    buffer: ArchiverBufferConfig = Field(default_factory=ArchiverBufferConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    routing: ArchiverRoutingConfig = Field(default_factory=ArchiverRoutingConfig)
    compression: CompressionConfig = Field(default_factory=CompressionConfig)
