#  Project:      dfe-engine
#  File:         kafka/cloud/base.py
#  Purpose:      ManagedKafkaProvider - the managed-Kafka control-plane interface
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""One lifecycle interface every managed-Kafka control-plane driver implements.

A managed Kafka cluster (Confluent Cloud, provisioned MSK, Redpanda Cloud) is
ALWAYS-ON - it bills continuously and has NO pause; only deletion stops spend.
See ``docs/deployment/managed-kafka-lifecycle.md`` for the full rationale. This module is
the per-provider SEAM (dfe-engine#99, WS-C): ``up`` creates + mints data-plane
creds, ``down`` tears down to PROVABLY EMPTY (creds die before the cluster,
never the other way round), ``status`` reads without mutating. Provider
selection is config/``--provider``, never hardcoded - nothing HyperI-specific
(dfe-engine is a product suite, not an internal tool).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


class ManagedKafkaProviderError(Exception):
    """A managed-Kafka control-plane call failed, or the provider is unusable
    (not configured, not yet built, out of scope)."""


@dataclass(slots=True)
class KafkaConnection:
    """The minted data-plane connection set for a managed Kafka cluster.

    Returned by :meth:`ManagedKafkaProvider.up` and handed straight to the
    shared cred-persistence helper (``dfe_engine.kafka.cloud.creds``) - never
    printed-for-manual-paste, unlike the ``.tmp`` scratch script this promotes.
    """

    cluster_id: str
    bootstrap_servers: str
    security_protocol: str
    sasl_mechanism: str
    username: str
    password: str
    # Provider-specific extras worth persisting (e.g. Redpanda's HTTP dataplane
    # endpoint, needed to mint/revoke users on a later `down`) but not part of
    # the common SASL connection shape.
    extra: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class KafkaClusterState:
    """A managed cluster's current control-plane state (``status`` / post-``down``).

    ``exists is False`` is the ONLY provably-empty / $0 state - a cluster in any
    other state is still billing (docs/deployment/managed-kafka-lifecycle.md: "the managed
    control plane keeps the cluster alive - and billing - until you delete it").
    """

    exists: bool
    cluster_id: str = ""
    state: str = ""
    bootstrap_servers: str = ""
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        """True when there is no cluster - the teardown-to-empty / $0 state."""
        return not self.exists


class ManagedKafkaProvider(ABC):
    """The up/down/status interface a managed-Kafka control-plane driver implements.

    Concrete implementations own their own auth (OAuth2 client-credentials,
    API-key, IAM, ...) and HTTP transport; the interface only fixes the THREE
    verbs the lifecycle CLI drives (dfe-engine#99). ``down`` MUST implement
    teardown-to-empty: delete data-plane creds BEFORE the cluster (an orphaned
    key is a support ticket, not a saving - the Confluent 403-orphan lesson),
    wait on the provider's own delete operation, then return a state whose
    ``is_empty`` is provably true (never inferred from a timer).
    """

    #: Provider id as passed to ``--provider`` (e.g. ``"redpanda-cloud"``).
    name: str

    @abstractmethod
    def up(self) -> KafkaConnection:
        """Create the cluster if absent (gate on provider-ready, never a timer),
        mint data-plane creds, and return the connection set to persist."""

    @abstractmethod
    def down(self) -> KafkaClusterState:
        """Teardown-to-empty: delete creds, then the cluster, then assert empty.

        Returns the POST-teardown state; a caller checks ``.is_empty`` rather
        than trusting a bare "it worked" - teardown that leaves a cluster
        standing is a defect, not a convenience.
        """

    @abstractmethod
    def status(self) -> KafkaClusterState:
        """Read the current control-plane state. Never mutates anything."""
