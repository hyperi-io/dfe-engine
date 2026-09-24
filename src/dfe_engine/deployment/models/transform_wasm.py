"""Deployment configuration model for dfe-transform-wasm."""

from __future__ import annotations

from pydantic import Field

from dfe_engine.deployment.models.common import (
    BaseDeploymentConfig,
    KedaConfig,
    TShirtSize,
)


class TransformWasmDeploymentConfig(BaseDeploymentConfig):
    """Deployment configuration for dfe-transform-wasm.

    Extends base with WASM-specific volume mounts for:
    - WASM module files per source
    - enrichment/associated files per source (CSV, MMDB, etc.)
    """

    size: TShirtSize = Field(
        default=TShirtSize.medium,
        description="T-shirt size for resource allocation",
    )
    image: str = "harbor.hyperi.io/dfe/dfe-transform-wasm"
    keda: KedaConfig = Field(
        default_factory=lambda: KedaConfig(enabled=False, min_replicas=1, max_replicas=8),
    )
    source_file_volumes: list[str] = Field(
        default_factory=list,
        description="K8s volume names for per-source associated files (WASM, CSV, MMDB, etc.)",
    )
