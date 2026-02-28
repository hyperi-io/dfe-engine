"""Base service configuration model.

All DFE service configs inherit from BaseServiceConfig, which provides
the universally shared fields: metrics and logging.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from dfe_engine.services.models.common import LoggingConfig, MetricsConfig


class BaseServiceConfig(BaseModel):
    """Abstract base for all DFE service runtime configurations.

    Every Rust service exposes Prometheus metrics and has structured logging.
    These are the only two fields truly universal across all services.
    Per-service config (Kafka, ClickHouse, routing, etc.) lives in subclasses.
    """

    model_config = ConfigDict(extra="forbid")

    metrics: MetricsConfig = Field(default_factory=MetricsConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
