"""Deployment configuration model for dfe-archiver."""

from __future__ import annotations

from pydantic import Field

from dfe_engine.deployment.models.common import (
    BaseDeploymentConfig,
    K8sServiceConfig,
    KedaConfig,
    TShirtSize,
)


class ArchiverDeploymentConfig(BaseDeploymentConfig):
    """Deployment configuration for dfe-archiver.

    Controls K8s resource sizing, autoscaling, and pod configuration.
    Service config (what the binary reads) is separate — see services/ module.
    """

    size: TShirtSize = Field(
        default=TShirtSize.small,
        description="T-shirt size for resource allocation",
    )
    image: str = "ghcr.io/hyperi-io/dfe-archiver"
    keda: KedaConfig = Field(
        default_factory=lambda: KedaConfig(enabled=False, min_replicas=1, max_replicas=4),
    )
    service: K8sServiceConfig = Field(
        default_factory=lambda: K8sServiceConfig(port=8080, metrics_port=9090),
    )
