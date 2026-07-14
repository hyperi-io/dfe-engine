"""Pydantic configuration models for DFE Rust services."""

from dfe_engine.services.models.archiver import (
    ArchiveConfig,
    ArchiverBufferConfig,
    ArchiverConfig,
    ArchiverKafkaConfig,
    ArchiverRoutingConfig,
    AzureConfig,
    CompressionConfig,
    GcsConfig,
    MinioConfig,
    S3Config,
)
from dfe_engine.services.models.base import BaseServiceConfig
from dfe_engine.services.models.common import (
    DlqConfig,
    KafkaTlsConfig,
    LoggingConfig,
    MemoryConfig,
    MetricsConfig,
    SaslConfig,
)
from dfe_engine.services.models.fetcher import (
    FetcherAuthConfig,
    FetcherConfig,
    FetcherKafkaConfig,
    FetcherRoutingConfig,
    FetcherSourceConfig,
)
from dfe_engine.services.models.loader import (
    AutoInitConfig,
    ClickHouseConfig,
    CoercionConfig,
    FieldSanitizationConfig,
    LoaderBufferConfig,
    LoaderConfig,
    LoaderDlqConfig,
    LoaderKafkaConfig,
    LoaderRoutingConfig,
    MetadataConfig,
    PayloadConfig,
    SchemaConfig,
    TimestampDqConfig,
)
from dfe_engine.services.models.receiver import (
    AcceptedHeader,
    AuthConfig,
    BearerConfig,
    DestinationRule,
    DestinationsConfig,
    GrpcConfig,
    LoaderConnectionConfig,
    ProducerConfig,
    ReceiverBufferConfig,
    ReceiverConfig,
    ReceiverKafkaConfig,
    ReceiverRoutingConfig,
    ServerConfig,
    SourceRule,
    TlsConfig,
    ValidationConfig,
)
from dfe_engine.services.models.source_common import (
    BaseSourceConfig,
    SourceFileConfig,
)
from dfe_engine.services.models.transform_vector import (
    TransformKafkaConfig,
    TransformVectorConfig,
    VectorSourceConfig,
)
from dfe_engine.services.models.transform_wasm import (
    TransformWasmConfig,
    WasmKafkaConfig,
    WasmSourceConfig,
)

# Service name constants
SERVICE_RECEIVER = "receiver"
SERVICE_LOADER = "loader"
SERVICE_ARCHIVER = "archiver"
SERVICE_TRANSFORM_VECTOR = "transform-vector"
SERVICE_TRANSFORM_WASM = "transform-wasm"
SERVICE_FETCHER = "fetcher"

# Backward-compatible static sets (prefer plugins.valid_services() for dynamic lookup)
VALID_SERVICES = {
    SERVICE_RECEIVER,
    SERVICE_LOADER,
    SERVICE_ARCHIVER,
    SERVICE_TRANSFORM_VECTOR,
    SERVICE_TRANSFORM_WASM,
    SERVICE_FETCHER,
}

# Backward-compatible config class mapping (prefer plugins.config_classes())
SERVICE_CONFIG_CLASSES: dict[str, type] = {
    SERVICE_RECEIVER: ReceiverConfig,
    SERVICE_LOADER: LoaderConfig,
    SERVICE_ARCHIVER: ArchiverConfig,
    SERVICE_TRANSFORM_VECTOR: TransformVectorConfig,
    SERVICE_TRANSFORM_WASM: TransformWasmConfig,
    SERVICE_FETCHER: FetcherConfig,
}

__all__ = [
    # Base
    "BaseServiceConfig",
    # Source common
    "BaseSourceConfig",
    "SourceFileConfig",
    # Constants
    "SERVICE_ARCHIVER",
    "SERVICE_CONFIG_CLASSES",
    "SERVICE_FETCHER",
    "SERVICE_LOADER",
    "SERVICE_RECEIVER",
    "SERVICE_TRANSFORM_VECTOR",
    "SERVICE_TRANSFORM_WASM",
    "VALID_SERVICES",
    # Common
    "DlqConfig",
    "KafkaTlsConfig",
    "LoggingConfig",
    "MemoryConfig",
    "MetricsConfig",
    "SaslConfig",
    # Receiver
    "AcceptedHeader",
    "AuthConfig",
    "BearerConfig",
    "DestinationRule",
    "DestinationsConfig",
    "GrpcConfig",
    "LoaderConnectionConfig",
    "ProducerConfig",
    "ReceiverBufferConfig",
    "ReceiverConfig",
    "ReceiverKafkaConfig",
    "ReceiverRoutingConfig",
    "ServerConfig",
    "SourceRule",
    "TlsConfig",
    "ValidationConfig",
    # Loader
    "AutoInitConfig",
    "ClickHouseConfig",
    "CoercionConfig",
    "FieldSanitizationConfig",
    "LoaderBufferConfig",
    "LoaderConfig",
    "LoaderDlqConfig",
    "LoaderKafkaConfig",
    "LoaderRoutingConfig",
    "MetadataConfig",
    "PayloadConfig",
    "SchemaConfig",
    "TimestampDqConfig",
    # Archiver
    "ArchiveConfig",
    "ArchiverBufferConfig",
    "ArchiverConfig",
    "ArchiverKafkaConfig",
    "ArchiverRoutingConfig",
    "AzureConfig",
    "CompressionConfig",
    "GcsConfig",
    "MinioConfig",
    "S3Config",
    # Transform Vector
    "TransformKafkaConfig",
    "TransformVectorConfig",
    "VectorSourceConfig",
    # Transform WASM
    "TransformWasmConfig",
    "WasmKafkaConfig",
    "WasmSourceConfig",
    # Fetcher
    "FetcherAuthConfig",
    "FetcherConfig",
    "FetcherKafkaConfig",
    "FetcherRoutingConfig",
    "FetcherSourceConfig",
]
