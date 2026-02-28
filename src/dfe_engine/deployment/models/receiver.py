"""Deployment configuration model for dfe-receiver."""

from __future__ import annotations

from pydantic import Field

from dfe_engine.deployment.models.common import (
    BaseDeploymentConfig,
    K8sServiceConfig,
    KedaConfig,
    KedaTriggerKafka,
    TShirtSize,
)


class ReceiverDeploymentConfig(BaseDeploymentConfig):
    """Deployment configuration for dfe-receiver.

    Controls K8s resource sizing, autoscaling, and pod configuration.
    Service config (what the binary reads) is separate — see services/ module.
    """

    size: TShirtSize = Field(
        default=TShirtSize.small,
        description="T-shirt size for resource allocation",
    )
    image: str = "harbor.hyperi.io/dfe/dfe-receiver"
    keda: KedaConfig = Field(
        default_factory=lambda: KedaConfig(
            enabled=False,
            min_replicas=2,
            max_replicas=10,
            kafka_trigger=KedaTriggerKafka(
                consumer_group="dfe-receiver",
                lag_threshold=100,
            ),
        ),
    )
    service: K8sServiceConfig = Field(
        default_factory=lambda: K8sServiceConfig(port=8080, metrics_port=9090),
    )
