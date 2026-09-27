"""Configuration model for dfe-transform-vrl.

A transform-vrl deployment embeds the VRL crate directly -- no Vector
subprocess. Config is simpler than transform-vector: flat source/sink
Kafka config, a VRL transforms directory or file list, and pipeline
settings (batch size, timeouts).
"""

from pydantic import BaseModel, ConfigDict, Field, model_validator

from dfe_engine.services.models.base import BaseServiceConfig
from dfe_engine.services.models.common import (
    AcknowledgementsConfig,
    KafkaTlsConfig,
    LoggingConfig,
    MetricsConfig,
    SaslConfig,
    without_keys,
)


class VrlSourceConfig(BaseModel):
    """Kafka source (consumer) configuration for VRL transforms."""

    model_config = ConfigDict(extra="forbid")

    brokers: list[str] = Field(default_factory=lambda: ["localhost:9092"])
    topics: list[str] = Field(default_factory=lambda: ["events"])
    group_id: str = "dfe-transform-vrl"
    max_buffer_bytes: int = Field(default=67_108_864, gt=0, description="Consumer buffer (bytes)")
    sasl: SaslConfig | None = None
    tls: KafkaTlsConfig | None = None
    auto_offset_reset: str = "latest"
    session_timeout_ms: int = Field(default=30_000, gt=0)
    commit_interval_ms: int = Field(default=5_000, gt=0)
    librdkafka_options: dict[str, str] = Field(default_factory=dict)
    acknowledgements: AcknowledgementsConfig = Field(default_factory=AcknowledgementsConfig)

    @model_validator(mode="before")
    @classmethod
    def _ignore_retired_format(cls, data: object) -> object:
        """Drop ``format``: a transform-vrl file stored before the key was retired still has it."""
        return without_keys(data, "format")


class VrlSinkConfig(BaseModel):
    """Kafka sink (producer) configuration for VRL transforms."""

    model_config = ConfigDict(extra="forbid")

    brokers: list[str] = Field(default_factory=lambda: ["localhost:9092"])
    topic: str = ""
    key_field: str = Field(
        default="", description="Event field for Kafka partition key (e.g. .org_id)"
    )
    compression: str = Field(default="zstd", description="none, gzip, lz4, snappy, zstd")
    max_buffer_bytes: int = Field(default=67_108_864, gt=0, description="Producer buffer (bytes)")
    sasl: SaslConfig | None = None
    tls: KafkaTlsConfig | None = None
    message_timeout_ms: int = Field(default=300_000, gt=0)
    librdkafka_options: dict[str, str] = Field(default_factory=dict)


class VrlPipelineConfig(BaseModel):
    """Pipeline identity and processing settings."""

    model_config = ConfigDict(extra="forbid")

    name: str = "default"
    batch_size: int = Field(default=1000, gt=0)
    batch_timeout_ms: int = Field(default=100, gt=0)


class VrlTransformConfig(BaseModel):
    """VRL transform file configuration."""

    model_config = ConfigDict(extra="forbid")

    dir: str | None = Field(
        default=None, description="Directory of .vrl files (sorted by filename)"
    )
    files: list[str] | None = Field(default=None, description="Explicit list of .vrl file paths")


class VrlHealthConfig(BaseModel):
    """Health endpoint configuration."""

    model_config = ConfigDict(extra="forbid")

    address: str = "0.0.0.0:9000"


class VrlScalingConfig(BaseModel):
    """Scaling pressure configuration."""

    model_config = ConfigDict(extra="forbid")

    pressure_threshold: float = Field(default=0.8, ge=0.0, le=1.0)


class TransformVrlConfig(BaseServiceConfig):
    """Complete configuration for dfe-transform-vrl.

    Unlike transform-vector, this is a flat config with no multi-source
    tree -- just a single source -> VRL transform -> sink pipeline.
    """

    pipeline: VrlPipelineConfig = Field(default_factory=VrlPipelineConfig)
    source: VrlSourceConfig = Field(default_factory=VrlSourceConfig)
    sink: VrlSinkConfig = Field(default_factory=VrlSinkConfig)
    transforms: VrlTransformConfig = Field(default_factory=VrlTransformConfig)
    health: VrlHealthConfig = Field(default_factory=VrlHealthConfig)
    metrics: MetricsConfig = Field(default_factory=MetricsConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    scaling: VrlScalingConfig = Field(default_factory=VrlScalingConfig)
