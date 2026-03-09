"""Environment configuration for Helm values compilation.

Deployment-scoped infrastructure details shared across all services
in one environment (e.g. production, staging).

Loaded from YAML (e.g. ``environments/production.yaml``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from dfe_engine.yaml_utils import yaml_load


class KafkaEnvironment(BaseModel):
    """Kafka infrastructure for a deployment environment."""

    model_config = ConfigDict(extra="forbid")

    bootstrap_servers: list[str] = Field(..., description="Concrete broker addresses")
    authentication_ref: str = Field(
        default="", description="KEDA TriggerAuthentication resource name"
    )


class ClickHouseEnvironment(BaseModel):
    """ClickHouse infrastructure for a deployment environment."""

    model_config = ConfigDict(extra="forbid")

    hosts: list[str] = Field(..., description="ClickHouse hosts")
    database: str = "default"
    username: str = "default"
    password: SecretStr = SecretStr("")
    secure: bool = True


class OTelEnvironment(BaseModel):
    """OpenTelemetry configuration for a deployment environment.

    When enabled, OTEL env vars are injected into all service Helm values.
    The OTEL Collector also exposes a Prometheus-compatible endpoint on
    ``prometheus_port`` for KEDA metrics scaling.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    collector_endpoint: str = Field(
        default="http://otel-collector:4317",
        description="OTLP gRPC endpoint",
    )
    protocol: str = Field(
        default="grpc",
        description="OTLP protocol: grpc or http/protobuf",
    )
    prometheus_port: int = Field(
        default=8889,
        description="Port where OTEL Collector exposes Prometheus metrics",
    )
    resource_attributes: dict[str, str] = Field(
        default_factory=dict,
        description="Extra OTEL resource attributes",
    )


class ArgoSyncPolicy(BaseModel):
    """Argo CD sync policy configuration."""

    model_config = ConfigDict(extra="forbid")

    auto_sync: bool = Field(default=True, description="Enable automated sync")
    prune: bool = Field(default=True, description="Auto-delete removed resources")
    self_heal: bool = Field(default=True, description="Auto-revert manual changes")
    retry_limit: int = Field(default=5, ge=0, description="Max sync retry attempts")
    sync_options: list[str] = Field(
        default_factory=lambda: ["CreateNamespace=true"],
        description="Argo CD syncOptions list",
    )

    def to_argo_dict(self) -> dict[str, Any]:
        """Convert to Argo CD syncPolicy dict format."""
        policy: dict[str, Any] = {}
        if self.auto_sync:
            policy["automated"] = {
                "prune": self.prune,
                "selfHeal": self.self_heal,
            }
        if self.sync_options:
            policy["syncOptions"] = list(self.sync_options)
        if self.retry_limit > 0:
            policy["retry"] = {
                "limit": self.retry_limit,
                "backoff": {
                    "duration": "5s",
                    "maxDuration": "3m",
                    "factor": 2,
                },
            }
        return policy


class ArgoEnvironment(BaseModel):
    """Argo CD configuration for a deployment environment.

    When ``enabled``, the compiler generates Application CRDs and an
    AppProject CRD alongside Helm values files.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(default=False, description="Generate Argo CD manifests")
    project: str = Field(default="dfe", description="Argo CD project name")
    chart_repo_url: str = Field(default="", description="Helm chart repository URL")
    chart_version: str = Field(
        default="", description="Default Helm chart version for all services"
    )
    source_repos: list[str] = Field(
        default_factory=list,
        description="Allowed source repos for AppProject",
    )
    destination_server: str = Field(
        default="https://kubernetes.default.svc",
        description="K8s API server URL",
    )
    values_path_prefix: str = Field(
        default="values",
        description="Directory prefix for values files in the config repo",
    )
    sync_policy: ArgoSyncPolicy = Field(
        default_factory=ArgoSyncPolicy,
        description="Default sync policy for all Applications",
    )
    chart_overrides: dict[str, str] = Field(
        default_factory=dict,
        description="Service name → chart name overrides (default: dfe-{service})",
    )
    ignore_differences: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Argo CD ignoreDifferences applied to all Applications",
    )
    labels: dict[str, str] = Field(
        default_factory=dict,
        description="Extra labels on all Application CRDs",
    )
    annotations: dict[str, str] = Field(
        default_factory=dict,
        description="Extra annotations on all Application CRDs",
    )


class ChartSource(BaseModel):
    """Helm chart source for an external component."""

    model_config = ConfigDict(extra="forbid")

    repo_url: str = Field(..., description="Helm chart repository URL")
    name: str = Field(..., description="Chart name (e.g. vector, kafbat-ui)")
    version: str = Field(default="", description="Chart version constraint")


class ExternalComponent(BaseModel):
    """An externally-sourced component managed via Helm + Argo CD.

    Mode 2 deployment: the chart comes from an upstream repository
    (e.g. vector.dev, kafbat, HyperDX). Engine provides a base values
    file per instance plus ``values_overrides`` deep-merged on top.

    Example YAML::

        components:
          - name: vector
            chart:
              repo_url: https://helm.vector.dev
              name: vector
              version: "0.42.1"
            namespace: dfe
            instances:
              receiver: values/vector-receiver.yaml
              pipelines: values/vector-pipelines.yaml
            values_overrides:
              receiver:
                customConfig:
                  api:
                    enabled: true
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., description="Component name (e.g. vector, kafbat-ui)")
    chart: ChartSource = Field(..., description="Upstream Helm chart source")
    namespace: str = Field(default="dfe", description="K8s namespace for this component")
    instances: dict[str, str] = Field(
        default_factory=dict,
        description="Instance name → base values file path",
    )
    values_overrides: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description="Instance name → values dict deep-merged onto base",
    )
    enabled: bool = Field(default=True, description="Whether this component is deployed")


class EnvironmentConfig(BaseModel):
    """Deployment environment configuration.

    Contains infrastructure connection details shared by all services
    in a single deployment environment.

    Services are deployed in two modes:
      - Mode 1 (DFE-managed): built from engine Pydantic models, chart in our repo
      - Mode 2 (External): upstream charts with base values + engine overrides

    Mode 2 components are listed under ``components``.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., description="Environment name (production, staging, dev)")
    namespace: str = Field(default="dfe", description="K8s namespace")
    kafka: KafkaEnvironment
    clickhouse: ClickHouseEnvironment
    image_registry: str = Field(
        default="harbor.hyperi.io/dfe", description="Container image registry"
    )
    image_tag_override: str | None = Field(
        default=None, description="Override image tag for all services"
    )
    otel: OTelEnvironment = Field(
        default_factory=OTelEnvironment, description="OpenTelemetry configuration"
    )
    secret_refs: dict[str, str] = Field(
        default_factory=dict, description="Logical name → K8s Secret name"
    )
    argo: ArgoEnvironment = Field(
        default_factory=ArgoEnvironment, description="Argo CD configuration"
    )
    components: list[ExternalComponent] = Field(
        default_factory=list,
        description="External (Mode 2) components — upstream charts with engine overrides",
    )

    @classmethod
    def from_yaml(cls, path: str | Path) -> EnvironmentConfig:
        """Load environment config from a YAML file."""
        data = yaml_load(Path(path))
        if data is None:
            msg = f"Empty or invalid environment config: {path}"
            raise ValueError(msg)
        return cls.model_validate(data)

    def to_dict(self) -> dict[str, Any]:
        """Export as dict, scrubbing secrets."""
        data = self.model_dump(mode="json")
        if "clickhouse" in data and "password" in data["clickhouse"]:
            data["clickhouse"]["password"] = "***"
        return data
