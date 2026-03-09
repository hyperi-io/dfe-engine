"""Configuration model for dfe-transform-wasm.

A WASM-based transform service. Like transform-vector but uses WASM modules
instead of vector.dev configs. Each source has a WASM module file and its
own ENV/files configuration.

Unlike transform-vector, WASM transforms have a flat source list (no tree).
The per-source ENV + files pattern is shared with transform-vector and fetcher.
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
# Kafka (both consumer and producer — transforms read and write)
# ---------------------------------------------------------------------------


class WasmKafkaConsumerConfig(BaseModel):
    """Kafka consumer configuration for the WASM transform input."""

    model_config = ConfigDict(extra="forbid")

    brokers: list[str] = Field(default_factory=lambda: ["localhost:9092"])
    group: str = "dfe-transform-wasm"
    topics: list[str] = Field(default_factory=list)
    topic_regex: str | None = None
    client_id: str = "dfe-transform-wasm"
    sasl: SaslConfig | None = None
    tls: KafkaTlsConfig | None = None


class WasmKafkaProducerConfig(BaseModel):
    """Kafka producer configuration for the WASM transform output."""

    model_config = ConfigDict(extra="forbid")

    brokers: list[str] = Field(default_factory=lambda: ["localhost:9092"])
    client_id: str = "dfe-transform-wasm"
    compression: str = "lz4"
    batch_size: int = Field(default=8 * 1024 * 1024, gt=0)
    linger_ms: int = Field(default=20, ge=0)
    sasl: SaslConfig | None = None
    tls: KafkaTlsConfig | None = None


class WasmKafkaConfig(BaseModel):
    """Combined Kafka consumer + producer config for WASM transforms."""

    model_config = ConfigDict(extra="forbid")

    consumer: WasmKafkaConsumerConfig = Field(default_factory=WasmKafkaConsumerConfig)
    producer: WasmKafkaProducerConfig = Field(default_factory=WasmKafkaProducerConfig)


# ---------------------------------------------------------------------------
# WASM source configs (flat list — no tree)
# ---------------------------------------------------------------------------


class WasmSourceConfig(BaseSourceConfig):
    """A single WASM transform source within a deployment.

    Extends BaseSourceConfig with WASM-specific fields:
    - wasm_module: path to compiled .wasm file
    - input_topic / output_topic: Kafka routing per source

    Flat list (no tree — tree is vector-only).
    Per-source ENV + files are inherited from BaseSourceConfig.
    """

    wasm_module: str = Field(..., description="Path to the compiled WASM module file (.wasm)")
    input_topic: str = Field(default="", description="Kafka topic to consume from")
    output_topic: str = Field(default="", description="Kafka topic to produce to")


# ---------------------------------------------------------------------------
# IPC (dev/test direct mode — no Kafka)
# ---------------------------------------------------------------------------


class WasmIpcConfig(BaseModel):
    """Direct IPC transport for dev/test (no Kafka dependency)."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    transport: str = Field(default="memory", description="memory, unix_socket, or tcp")
    address: str = ""


# ---------------------------------------------------------------------------
# Top-level config
# ---------------------------------------------------------------------------


class TransformWasmConfig(BaseServiceConfig):
    """Complete configuration for dfe-transform-wasm.

    Like transform-vector but uses WASM modules for transform logic.
    Flat source list (no config tree). Each source has its own ENV
    and associated files, CRUD-managed via the engine API.
    """

    kafka: WasmKafkaConfig = Field(default_factory=WasmKafkaConfig)
    sources: list[WasmSourceConfig] = Field(
        default_factory=list,
        description="List of WASM source configurations",
    )
    extra_env: dict[str, str] = Field(
        default_factory=dict,
        description="Additional environment variables for the transform process",
    )
    ipc: WasmIpcConfig = Field(
        default_factory=WasmIpcConfig,
        description="Direct IPC transport for dev/test (no Kafka)",
    )
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
