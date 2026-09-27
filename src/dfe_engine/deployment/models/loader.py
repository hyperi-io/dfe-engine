"""Deployment configuration model for dfe-loader."""

from __future__ import annotations

from pydantic import Field

from dfe_engine.deployment.models.common import (
    BaseDeploymentConfig,
    K8sServiceConfig,
    KedaConfig,
    TShirtSize,
)


class LoaderDeploymentConfig(BaseDeploymentConfig):
    """Deployment configuration for dfe-loader.

    Controls K8s resource sizing, autoscaling, and pod configuration.
    Service config (what the binary reads) is separate — see services/ module.
    """

    size: TShirtSize = Field(
        default=TShirtSize.medium,
        description="T-shirt size for resource allocation",
    )
    image: str = "ghcr.io/hyperi-io/dfe-loader"
    keda: KedaConfig = Field(
        default_factory=lambda: KedaConfig(enabled=False, min_replicas=2, max_replicas=8),
    )
    service: K8sServiceConfig = Field(
        default_factory=lambda: K8sServiceConfig(port=9000, metrics_port=9090),
    )
