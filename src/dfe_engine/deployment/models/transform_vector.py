"""Deployment configuration model for dfe-transform-vector."""

from __future__ import annotations

from pydantic import Field

from dfe_engine.deployment.models.common import (
    BaseDeploymentConfig,
    KedaConfig,
    TShirtSize,
)


class TransformVectorDeploymentConfig(BaseDeploymentConfig):
    """Deployment configuration for dfe-transform-vector.

    Extends base with vector-specific volume mounts for:
    - extra YAML config files (concatenated into the vector.dev config)
    - enrichment/associated files per source (CSV, MMDB, etc.)
    """

    size: TShirtSize = Field(
        default=TShirtSize.medium,
        description="T-shirt size for resource allocation",
    )
    image: str = "harbor.hyperi.io/dfe/dfe-transform-vector"
    keda: KedaConfig = Field(
        default_factory=lambda: KedaConfig(enabled=False, min_replicas=1, max_replicas=8),
    )
    extra_yaml_volumes: list[str] = Field(
        default_factory=list,
        description="K8s volume names for additional vector.dev YAML config files",
    )
    source_file_volumes: list[str] = Field(
        default_factory=list,
        description="K8s volume names for per-source associated files (CSV, MMDB, etc.)",
    )
