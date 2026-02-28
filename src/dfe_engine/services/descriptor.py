"""Static service descriptors for DFE services.

ServiceDescriptor holds compile-time constants that never change at runtime:
image name, default ports, health paths, Kafka role, etc.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class KafkaRole(str, Enum):
    """Kafka interaction role for a service."""

    PRODUCER = "producer"
    CONSUMER = "consumer"
    BOTH = "both"
    NONE = "none"


@dataclass(frozen=True)
class ServiceDescriptor:
    """Immutable metadata about a DFE service.

    This is compile-time data — not user-editable config. It describes
    the service's identity, container image, networking, and health probes.

    Attributes:
        name: Service name (e.g. "receiver", "loader", "transformer").
        display_name: Human-friendly name (e.g. "DFE Receiver").
        image: Default container image (e.g. "harbor.hyperi.io/dfe/dfe-receiver").
        default_port: Primary service port.
        metrics_port: Prometheus metrics port.
        kafka_role: Whether the service produces, consumes, or both.
        consumer_group: Default Kafka consumer group (if consumer/both).
        liveness_paths: HTTP paths for liveness probes.
        readiness_paths: HTTP paths for readiness probes.
        description: Short description of the service.
    """

    name: str
    display_name: str
    image: str
    default_port: int = 8080
    metrics_port: int = 9090
    kafka_role: KafkaRole = KafkaRole.NONE
    consumer_group: str = ""
    liveness_paths: tuple[str, ...] = ("/health/live",)
    readiness_paths: tuple[str, ...] = ("/health/ready",)
    description: str = ""
    extra_ports: dict[str, int] = field(default_factory=dict)
