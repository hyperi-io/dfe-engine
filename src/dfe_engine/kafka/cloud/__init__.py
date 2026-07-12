#  Project:      dfe-engine
#  File:         kafka/cloud/__init__.py
#  Purpose:      Managed-Kafka cluster lifecycle (WS-C, dfe-engine#99)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Managed-Kafka cluster lifecycle - one ``up``/``down``/``status`` seam per
provider behind :class:`~dfe_engine.kafka.cloud.base.ManagedKafkaProvider`.

See ``docs/MANAGED-KAFKA-LIFECYCLE.md`` for the design rationale (always-on
cost problem; ``down`` = DELETE not pause; teardown-to-empty). Redpanda Cloud
Serverless is the first (proven) provider, promoted from the
``.tmp/redpanda_lifecycle.py`` scratch driver.
"""

from __future__ import annotations

from .base import (
    KafkaClusterState,
    KafkaConnection,
    ManagedKafkaProvider,
    ManagedKafkaProviderError,
)

__all__ = [
    "KafkaClusterState",
    "KafkaConnection",
    "ManagedKafkaProvider",
    "ManagedKafkaProviderError",
]
