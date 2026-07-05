"""Shared deployment configuration models for DFE services.

Models K8s resource sizing, KEDA autoscaling, HPA fallback, and pod configuration.
Used by per-service deployment configs (receiver, loader, archiver).
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TShirtSize(str, Enum):
    """T-shirt sizing for K8s resource allocation.

    Uniform 2 GiB : 1 CPU ratio (C-series aligned).
    Each size is 2x the previous. Request = half of limit (burstable).
    """

    xs = "xs"
    small = "small"
    medium = "medium"
    large = "large"
    xlarge = "xlarge"
    custom = "custom"


class ResourceQuantity(BaseModel):
    """K8s resource quantity pair (CPU + memory)."""

    model_config = ConfigDict(extra="forbid")

    cpu: str = Field(description="CPU in K8s format (e.g. '500m', '2')")
    memory: str = Field(description="Memory in K8s format (e.g. '1Gi', '512Mi')")


class ResourceSpec(BaseModel):
    """K8s resource requests and limits."""

    model_config = ConfigDict(extra="forbid")

    requests: ResourceQuantity
    limits: ResourceQuantity


class KedaTriggerKafka(BaseModel):
    """KEDA Kafka consumer lag trigger."""

    model_config = ConfigDict(extra="forbid")

    consumer_group: str = ""
    topic: str = ""
    lag_threshold: int = Field(default=1000, gt=0)
    authentication_ref: str = Field(default="", description="TriggerAuthentication resource name")


class KedaTriggerCpu(BaseModel):
    """KEDA CPU utilization trigger."""

    model_config = ConfigDict(extra="forbid")

    metric_type: str = Field(default="Utilization", description="Utilization or Value")
    value: int = Field(default=80, ge=1, le=100, description="Target CPU percentage")

    @field_validator("metric_type")
    @classmethod
    def validate_metric_type(cls, v: str) -> str:
        allowed = {"Utilization", "Value"}
        if v not in allowed:
            msg = f"Invalid metric_type: {v}. Allowed: {', '.join(sorted(allowed))}"
            raise ValueError(msg)
        return v


class KedaTriggerPrometheus(BaseModel):
    """KEDA Prometheus/OTEL metrics trigger.

    KEDA's prometheus scaler queries a Prometheus-compatible endpoint.
    The OTEL Collector exposes Prometheus metrics on its metrics endpoint,
    so this works for both Prometheus and OTEL-based metrics.
    """

    model_config = ConfigDict(extra="forbid")

    server_address: str = Field(
        default="",
        description="Prometheus-compatible endpoint (e.g. http://otel-collector:8889)",
    )
    query: str = Field(
        default="",
        description="PromQL query (e.g. sum(rate(dfe_receiver_events_total[5m])))",
    )
    threshold: int = Field(default=100, gt=0)
    activation_threshold: int = Field(
        default=0, ge=0, description="Value below which scaler is inactive"
    )
    metric_name: str = Field(default="", description="Custom metric name for KEDA")


class KedaTriggerGeneric(BaseModel):
    """Generic KEDA trigger for any scaler type.

    Use this for KEDA scalers not covered by the typed trigger models.
    The type and metadata are passed through to the Helm values as-is.
    See https://keda.sh/docs/scalers/ for available types.
    """

    model_config = ConfigDict(extra="forbid")

    type: str = Field(..., description="KEDA scaler type (e.g. 'cron', 'rabbitmq')")
    metadata: dict[str, str] = Field(
        default_factory=dict, description="Scaler metadata key-value pairs"
    )
    authentication_ref: str = Field(default="", description="TriggerAuthentication resource name")


class KedaConfig(BaseModel):
    """KEDA ScaledObject configuration."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    min_replicas: int = Field(default=1, ge=0)
    max_replicas: int = Field(default=4, ge=1)
    polling_interval: int = Field(default=30, ge=1, description="Seconds between KEDA polls")
    cooldown_period: int = Field(default=300, ge=0, description="Seconds before scale-down")
    kafka_trigger: KedaTriggerKafka | None = None
    cpu_trigger: KedaTriggerCpu | None = None
    prometheus_trigger: KedaTriggerPrometheus | None = None
    extra_triggers: list[KedaTriggerGeneric] = Field(
        default_factory=list,
        description="Additional KEDA triggers (any scaler type)",
    )
    fallback_replicas: int = Field(default=2, ge=1, description="Replicas when all triggers fail")


class HpaConfig(BaseModel):
    """Horizontal Pod Autoscaler (fallback when KEDA not available)."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    min_replicas: int = Field(default=1, ge=1)
    max_replicas: int = Field(default=4, ge=1)
    target_cpu_percent: int = Field(default=80, ge=1, le=100)


class PodConfig(BaseModel):
    """Pod-level K8s configuration."""

    model_config = ConfigDict(extra="forbid")

    annotations: dict[str, str] = Field(default_factory=dict)
    labels: dict[str, str] = Field(default_factory=dict)
    node_selector: dict[str, str] = Field(default_factory=dict)
    tolerations: list[dict[str, str]] = Field(default_factory=list)
    affinity: dict = Field(default_factory=dict)
    image_pull_policy: str = Field(
        default="IfNotPresent",
        description="Container imagePullPolicy (Always/IfNotPresent/Never)",
    )


class K8sServiceConfig(BaseModel):
    """K8s Service resource configuration."""

    model_config = ConfigDict(extra="forbid")

    type: str = Field(default="ClusterIP", description="ClusterIP, NodePort, LoadBalancer")
    port: int = Field(default=8080, ge=1, le=65535)
    metrics_port: int = Field(default=9090, ge=1, le=65535)

    @field_validator("type")
    @classmethod
    def validate_type(cls, v: str) -> str:
        allowed = {"ClusterIP", "NodePort", "LoadBalancer"}
        if v not in allowed:
            msg = f"Invalid service type: {v}. Allowed: {', '.join(sorted(allowed))}"
            raise ValueError(msg)
        return v


class SecretRef(BaseModel):
    """Reference to a K8s Secret."""

    model_config = ConfigDict(extra="forbid")

    name: str = ""
    key: str = ""


class BaseDeploymentConfig(BaseModel):
    """Abstract base for all DFE service deployment configurations.

    Every service deployment has sizing, replicas, image, autoscaling,
    pod config, and a K8s service. Per-service differences (default image,
    port, KEDA triggers) are set in subclass defaults.
    """

    model_config = ConfigDict(extra="forbid")

    size: TShirtSize = Field(
        default=TShirtSize.small,
        description="T-shirt size for resource allocation",
    )
    replicas: int = Field(default=1, ge=0)
    image: str = "harbor.hyperi.io/dfe/dfe-service"
    image_tag: str = "latest"
    resources: ResourceSpec | None = Field(
        default=None,
        description="Custom resources (overrides size when size=custom)",
    )
    keda: KedaConfig = Field(default_factory=KedaConfig)
    hpa: HpaConfig = Field(default_factory=HpaConfig)
    pod: PodConfig = Field(default_factory=PodConfig)
    service: K8sServiceConfig = Field(default_factory=K8sServiceConfig)
    config_secret: SecretRef = Field(
        default_factory=SecretRef,
        description="K8s Secret containing sensitive config values",
    )
    extra_env: dict[str, str] = Field(
        default_factory=dict,
        description="Additional environment variables for the pod",
    )
