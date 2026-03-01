"""Pydantic models for Helm values output.

Maps 1:1 to what the Helm chart expects in ``values.yaml``.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class HelmKedaTrigger(BaseModel):
    """Fully-resolved KEDA trigger with concrete bootstrap servers."""

    model_config = ConfigDict(extra="forbid")

    type: str = "kafka"
    metadata: dict[str, str] = Field(default_factory=dict)
    authentication_ref: str = ""


class HelmKedaConfig(BaseModel):
    """KEDA ScaledObject configuration for Helm values."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    min_replicas: int = 1
    max_replicas: int = 4
    polling_interval: int = 30
    cooldown_period: int = 300
    fallback_replicas: int = 2
    triggers: list[HelmKedaTrigger] = Field(default_factory=list)


class HelmServiceValues(BaseModel):
    """Complete values.yaml for one service instance."""

    image: str
    image_tag: str = "latest"
    replicas: int = 1
    resources: dict[str, Any] = Field(default_factory=dict)
    pod: dict[str, Any] = Field(default_factory=dict)
    k8s_service: dict[str, Any] = Field(default_factory=dict)
    keda: HelmKedaConfig = Field(default_factory=HelmKedaConfig)
    hpa: dict[str, Any] = Field(default_factory=dict)
    config: dict[str, Any] = Field(
        default_factory=dict, description="Service runtime config → ConfigMap"
    )
    config_files: dict[str, str] = Field(
        default_factory=dict, description="Supplementary files → ConfigMaps"
    )
    secret_refs: dict[str, str] = Field(default_factory=dict)
    extra_env: dict[str, str] = Field(default_factory=dict)


class CompilationResult(BaseModel):
    """Result of compiling Helm values for all services."""

    helm_values: dict[str, HelmServiceValues] = Field(
        default_factory=dict,
        description="service-instance key → HelmServiceValues",
    )
    ddl_statements: list[str] = Field(default_factory=list)
    kafka_topics: list[dict[str, Any]] = Field(default_factory=list)
    argo_rbac_csv: str = Field(
        default="", description="Generated argocd-rbac-cm policy.csv content"
    )
    argo_appproject_roles: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Generated AppProject .spec.roles structure",
    )
    argo_applications: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Generated Argo CD Application CRDs",
    )
    argo_appproject: dict[str, Any] = Field(
        default_factory=dict,
        description="Generated Argo CD AppProject CRD",
    )
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
