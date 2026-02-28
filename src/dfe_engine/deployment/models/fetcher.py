"""Deployment configuration model for dfe-fetcher."""

from __future__ import annotations

from pydantic import Field

from dfe_engine.deployment.models.common import (
    BaseDeploymentConfig,
    K8sServiceConfig,
    KedaConfig,
    TShirtSize,
)


class FetcherDeploymentConfig(BaseDeploymentConfig):
    """Deployment configuration for dfe-fetcher.

    Like receiver but pulls from SaaS APIs. No KEDA Kafka trigger
    by default (fetcher is the data source, not a consumer).
    """

    size: TShirtSize = Field(
        default=TShirtSize.small,
        description="T-shirt size for resource allocation",
    )
    image: str = "harbor.hyperi.io/dfe/dfe-fetcher"
    keda: KedaConfig = Field(
        default_factory=lambda: KedaConfig(
            enabled=False,
            min_replicas=1,
            max_replicas=4,
        ),
    )
    service: K8sServiceConfig = Field(
        default_factory=lambda: K8sServiceConfig(port=8080, metrics_port=9090),
    )
    source_file_volumes: list[str] = Field(
        default_factory=list,
        description="K8s volume names for per-source associated files",
    )
