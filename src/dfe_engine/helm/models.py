# ruff: noqa: N815 -- field names intentionally mirror the chart's values.yaml
# keys (camelCase: replicaCount, minReplicaCount, nodeScheduling, ...) so the
# engine's overlay drops straight onto the base chart. They ARE the YAML schema.
"""Pydantic models for Helm values output.

These models ARE the values.yaml schema the dfe-infra charts consume -- field
names are deliberately the chart's keys (camelCase where the chart uses them:
``replicaCount``, ``minReplicaCount``, ``nodeScheduling``) so the engine's
published overlay drops straight onto the base chart via Argo multi-source. The
engine writes only the overlay; the chart ships the defaults.

Deployment dials (Layer A) are uniform across every dfe-* chart (dfe-common
conventions). App config (Layer B) is a passthrough blob: the engine emits
``config`` and the chart mounts it as the app's scalo config -- the chart never
needs to know per-app config keys (no coupling to chart internals).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class HelmImage(BaseModel):
    """``image`` block -- matches dfe-common.image (global.registry + repository:tag)."""

    repository: str = ""
    tag: str = ""
    pullPolicy: str = "IfNotPresent"


class HelmKedaTrigger(BaseModel):
    """One KEDA trigger, verbatim ScaledObject shape (type + metadata + auth ref)."""

    model_config = ConfigDict(extra="forbid")

    type: str = "metrics-api"
    metadata: dict[str, str] = Field(default_factory=dict)
    # {name: <fullname>-trigger-auth} when the trigger needs SASL auth; omitted otherwise.
    authenticationRef: dict[str, str] | None = None


class HelmKedaTriggerAuth(BaseModel):
    """KEDA TriggerAuthentication secretTargetRef list (Kafka SASL etc.)."""

    model_config = ConfigDict(extra="forbid")

    secretTargetRef: list[dict[str, str]] = Field(default_factory=list)


class HelmKeda(BaseModel):
    """``keda`` block consumed by dfe-common.scaledobject (folded into each chart)."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    minReplicaCount: int = 1
    maxReplicaCount: int = 10
    cooldownPeriod: int = 300
    pollingInterval: int = 30
    # None => omit from the overlay so the chart's default trigger (the gated
    # ScalingPressure metrics-api trigger) stands. Helm replaces lists rather than
    # merging, so emitting [] here would WIPE the chart default -- hence Optional.
    triggers: list[HelmKedaTrigger] | None = None
    triggerAuthentication: HelmKedaTriggerAuth | None = None


class HelmDeployMeta(BaseModel):
    """Fan-out metadata read by the Argo git-files generator (not consumed by the
    chart). ``service`` is the chart name (dfe-<svc>); ``instance`` the deploy
    instance. One Application per values file -> name <service>-<instance>."""

    model_config = ConfigDict(extra="forbid")

    service: str
    instance: str


class HelmServiceValues(BaseModel):
    """Complete overlay values.yaml for one service instance -- chart-shaped."""

    deploy: HelmDeployMeta
    image: HelmImage = Field(default_factory=HelmImage)
    replicaCount: int = 1
    resources: dict[str, Any] = Field(default_factory=dict)
    keda: HelmKeda = Field(default_factory=HelmKeda)
    nodeScheduling: dict[str, Any] = Field(
        default_factory=dict, description="{nodeSelector, tolerations} -- dfe-common.scheduling"
    )
    # App config passthrough: the chart mounts this as the app's scalo config; the
    # engine never needs the chart's per-app keys (Layer B = passthrough, no coupling).
    config: dict[str, Any] = Field(default_factory=dict)
    secret_refs: dict[str, str] = Field(default_factory=dict)
    extra_env: dict[str, str] = Field(default_factory=dict)


class CompilationResult(BaseModel):
    """Result of compiling Helm values for all services.

    NOTE: ``argo_*`` are still GENERATED for now but no longer PUBLISHED to the
    deploy repo -- dfe-infra's ApplicationSets own deployment; the engine writes
    only overlay values + DDL (see gitops/artifacts.py). The argo_* generation is
    dead-weight to be removed once the appset git-generator is the sole path.
    """

    helm_values: dict[str, HelmServiceValues] = Field(
        default_factory=dict,
        description="service-instance key -> HelmServiceValues (overlay)",
    )
    ddl_statements: list[str] = Field(default_factory=list)
    kafka_topics: list[dict[str, Any]] = Field(default_factory=list)
    argo_rbac_csv: str = Field(default="")
    argo_appproject_roles: list[dict[str, Any]] = Field(default_factory=list)
    argo_applications: list[dict[str, Any]] = Field(default_factory=list)
    argo_appproject: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
