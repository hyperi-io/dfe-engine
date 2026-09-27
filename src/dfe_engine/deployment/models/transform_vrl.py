"""Deployment configuration model for dfe-transform-vrl."""

from __future__ import annotations

from pydantic import Field

from dfe_engine.deployment.models.common import (
    BaseDeploymentConfig,
    KedaConfig,
    TShirtSize,
)


class TransformVrlDeploymentConfig(BaseDeploymentConfig):
    """Deployment configuration for dfe-transform-vrl.

    Simpler than transform-vector -- no extra YAML volumes or source
    file volumes. Just the binary, config, and VRL transforms directory.
    """

    size: TShirtSize = Field(
        default=TShirtSize.small,
        description="T-shirt size for resource allocation",
    )
    image: str = "ghcr.io/hyperi-io/dfe-transform-vrl"
    keda: KedaConfig = Field(
        default_factory=lambda: KedaConfig(enabled=False, min_replicas=1, max_replicas=10),
    )
    transforms_volume: str = Field(
        default="",
        description="K8s volume name for VRL transform files (mounted to /etc/dfe/transforms)",
    )
