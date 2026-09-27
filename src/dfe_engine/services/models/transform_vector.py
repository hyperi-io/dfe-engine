"""Configuration model for dfe-transform-vector.

A transform-vector deployment wraps one or more vector.dev processes.
Unlike vanilla services (receiver, loader, archiver) which have a single
flat config, a transform-vector deployment manages a tree of source configs:

- Each source has a vector.dev YAML config file defining its upstream
- Each source can have its own ENV overrides and associated files (CSV, MMDB)
- One YAML can be the source for multiple child transforms (DAG)
- The tree of YAML config files is specific to vector transforms

The top-level TransformVectorConfig contains:
- Common service config (Kafka consumer/producer, metrics, logging)
- A list of VectorSourceConfig entries (the config tree)
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from dfe_engine.services.models.base import BaseServiceConfig
from dfe_engine.services.models.common import (
    KafkaTlsConfig,
    MemoryConfig,
    SaslConfig,
)
from dfe_engine.services.models.source_common import BaseSourceConfig

# ---------------------------------------------------------------------------
# Kafka (both consumer and producer -- transforms read and write)
# ---------------------------------------------------------------------------


class TransformKafkaConsumerConfig(BaseModel):
    """Kafka consumer configuration for the transform input."""

    model_config = ConfigDict(extra="forbid")

    brokers: list[str] = Field(default_factory=lambda: ["localhost:9092"])
    group: str = "dfe-transform-vector"
    topics: list[str] = Field(default_factory=list)
    topic_regex: str | None = None
    client_id: str = "dfe-transform-vector"
    sasl: SaslConfig | None = None
    tls: KafkaTlsConfig | None = None


class TransformKafkaProducerConfig(BaseModel):
    """Kafka producer configuration for the transform output."""

    model_config = ConfigDict(extra="forbid")

    brokers: list[str] = Field(default_factory=lambda: ["localhost:9092"])
    client_id: str = "dfe-transform-vector"
    compression: str = "lz4"
    batch_size: int = Field(default=8 * 1024 * 1024, gt=0)
    linger_ms: int = Field(default=20, ge=0)
    sasl: SaslConfig | None = None
    tls: KafkaTlsConfig | None = None


class TransformKafkaConfig(BaseModel):
    """Combined Kafka consumer + producer config for transforms."""

    model_config = ConfigDict(extra="forbid")

    consumer: TransformKafkaConsumerConfig = Field(default_factory=TransformKafkaConsumerConfig)
    producer: TransformKafkaProducerConfig = Field(default_factory=TransformKafkaProducerConfig)


# ---------------------------------------------------------------------------
# Vector source configs (the multi-source tree -- vector-specific)
# ---------------------------------------------------------------------------


class VectorSourceConfig(BaseSourceConfig):
    """A single vector.dev source configuration within a transform deployment.

    Extends BaseSourceConfig with vector-specific fields:
    - config_file: the vector.dev YAML config
    - parent: tree structure (one YAML -> multiple child transforms)

    The tree of YAML config files is specific to vector transforms and
    is NOT a reused pattern across other multi-source services.
    """

    config_file: str = Field(..., description="Path to vector.dev YAML config file")
    parent: str = Field(
        default="",
        description="Parent source name (empty = root source in the config tree)",
    )


# ---------------------------------------------------------------------------
# IPC (dev/test direct mode -- no Kafka)
# ---------------------------------------------------------------------------


class IpcConfig(BaseModel):
    """Direct IPC transport for dev/test (no Kafka dependency)."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    transport: str = Field(default="memory", description="memory, unix_socket, or tcp")
    address: str = ""


# ---------------------------------------------------------------------------
# Top-level config
# ---------------------------------------------------------------------------


class TransformVectorConfig(BaseServiceConfig):
    """Complete configuration for dfe-transform-vector.

    Unlike vanilla services, a transform-vector deployment manages a tree
    of vector.dev source configs. Each source has its own ENV and associated
    files, enabling complex multi-stage transform pipelines.

    Sources and their ENV/files are CRUD-managed via the engine API,
    called by the control plane REST API.
    """

    kafka: TransformKafkaConfig = Field(default_factory=TransformKafkaConfig)
    sources: list[VectorSourceConfig] = Field(
        default_factory=list,
        description="List of vector.dev source configurations (the config tree)",
    )
    extra_yaml_files: list[str] = Field(
        default_factory=list,
        description="Additional YAML files to concatenate into the vector.dev config",
    )
    extra_env: dict[str, str] = Field(
        default_factory=dict,
        description="Additional environment variables for the transform process",
    )
    ipc: IpcConfig = Field(
        default_factory=IpcConfig,
        description="Direct IPC transport for dev/test (no Kafka)",
    )
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
