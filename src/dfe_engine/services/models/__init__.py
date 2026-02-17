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
from dfe_engine.services.models.common import (
    DlqConfig,
    KafkaTlsConfig,
    LoggingConfig,
    MemoryConfig,
    MetricsConfig,
    SaslConfig,
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
    TlsConfig,
    ValidationConfig,
)

# Service name constants
SERVICE_RECEIVER = "receiver"
SERVICE_LOADER = "loader"
SERVICE_ARCHIVER = "archiver"
VALID_SERVICES = {SERVICE_RECEIVER, SERVICE_LOADER, SERVICE_ARCHIVER}

# Service name → config class mapping
SERVICE_CONFIG_CLASSES: dict[str, type] = {
    SERVICE_RECEIVER: ReceiverConfig,
    SERVICE_LOADER: LoaderConfig,
    SERVICE_ARCHIVER: ArchiverConfig,
}

__all__ = [
    # Constants
    "SERVICE_ARCHIVER",
    "SERVICE_CONFIG_CLASSES",
    "SERVICE_LOADER",
    "SERVICE_RECEIVER",
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
]
