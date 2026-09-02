#  Project:      dfe-engine
#  File:         kafka/topics.py
#  Purpose:      Explicit Kafka topic admin - the topics a Source needs, created
#                by DFE rather than left to broker auto-create. dfe-engine#97.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Topic admin shared by the CLI, the Helm compiler and the source deploy hook.

DFE creates the topics its sources need EXPLICITLY. Relying on the broker's
``auto.create.topics.enable`` silently produces mis-partitioned, unmanaged
topics on first produce, which is the anti-pattern ``dfe kafka topics`` exists
to replace - so nothing in the engine may depend on it.

Every source needs ``<source>_land`` (raw, receiver -> Kafka); a source with a
transform also needs ``<source>_load`` (transformed, transform -> loader). That
``_land``/``_load`` convention is shared with scalo-rs and computed by
``Source.topic_land`` / ``Source.topic_load``.

The admin config is built from the credential contract (``kafka.contract``), so
the provider DERIVES security.protocol and sasl.mechanism. Building it any other
way loses SASL and fails against every DFE-owned broker, which is SCRAM by
standard.

``scalo.kafka.admin.KafkaAdmin`` is the published admin primitive, but as of the
scalo pin here it exposes only config-alter operations on topics that already
exist - not create/delete/list. This module fills that gap against the same
underlying ``confluent_kafka`` AdminClient, built with the identical config
shape. Collapse onto KafkaAdmin once scalo grows that surface.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from confluent_kafka.admin import AdminClient, NewTopic
from scalo.logger import logger

if TYPE_CHECKING:
    from dfe_engine.settings import Settings
    from dfe_engine.source.models import Source


@dataclass
class TopicSpec:
    """One topic DFE intends to exist."""

    name: str
    partitions: int
    replication_factor: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "partitions": self.partitions,
            "replication_factor": self.replication_factor,
        }


@dataclass
class TopicEnsureResult:
    """Outcome of an ensure pass: every requested topic lands in exactly one list."""

    created: list[str] = field(default_factory=list)
    existing: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed


def admin_config(
    *,
    bootstrap: str | None = None,
    provider: str | None = None,
    username: str | None = None,
    password: str | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Build the librdkafka admin config, sourced from settings unless overridden.

    The provider DERIVES protocol and mechanism via the credential contract
    (dfe-engine#98); never hand-set the mechanism.
    """
    from dfe_engine.kafka import contract
    from dfe_engine.settings import get_settings

    ks = (settings or get_settings()).kafka
    bootstrap = bootstrap or ks.bootstrap_servers
    provider = provider if provider is not None else ks.provider
    username = username if username is not None else ks.sasl_username
    password = password if password is not None else ks.sasl_password

    protocol, mechanism = (
        contract.derive(provider) if provider else (ks.security_protocol, ks.sasl_mechanism)
    )

    conf: dict[str, Any] = {"bootstrap.servers": bootstrap}
    if protocol and protocol.upper() != "PLAINTEXT":
        conf["security.protocol"] = protocol
    if mechanism:
        conf["sasl.mechanisms"] = mechanism
        conf["sasl.username"] = username or ""
        conf["sasl.password"] = password or ""
    return conf


class TopicAdmin:
    """Thin adapter over confluent_kafka's AdminClient for topic CRUD."""

    def __init__(self, config: dict[str, Any]) -> None:
        self._admin = AdminClient(config)

    def list_topic_names(self, *, timeout: float = 10.0) -> set[str]:
        metadata = self._admin.list_topics(timeout=timeout)
        return set(metadata.topics.keys())

    def create(
        self, name: str, *, partitions: int, replication_factor: int, timeout: float = 30.0
    ) -> None:
        new_topic = NewTopic(name, num_partitions=partitions, replication_factor=replication_factor)
        futures = self._admin.create_topics([new_topic], request_timeout=timeout)
        futures[name].result()

    def delete(self, name: str, *, timeout: float = 30.0) -> None:
        futures = self._admin.delete_topics([name], request_timeout=timeout)
        futures[name].result()


def build_admin(
    *,
    bootstrap: str | None = None,
    provider: str | None = None,
    username: str | None = None,
    password: str | None = None,
    settings: Settings | None = None,
) -> TopicAdmin:
    return TopicAdmin(
        admin_config(
            bootstrap=bootstrap,
            provider=provider,
            username=username,
            password=password,
            settings=settings,
        )
    )


def source_topic_specs(
    source: Source,
    *,
    partitions: int,
    replication_factor: int,
) -> list[TopicSpec]:
    """The topics one source needs: always ``_land``, plus ``_load`` if it transforms."""
    specs = [
        TopicSpec(
            name=source.topic_land,
            partitions=partitions,
            replication_factor=replication_factor,
        )
    ]
    if source.topic_load:
        specs.append(
            TopicSpec(
                name=source.topic_load,
                partitions=partitions,
                replication_factor=replication_factor,
            )
        )
    return specs


def ensure_topics(
    specs: list[TopicSpec],
    *,
    admin: TopicAdmin | None = None,
    settings: Settings | None = None,
    bootstrap: str | None = None,
    dry_run: bool = False,
) -> TopicEnsureResult:
    """Create every spec that does not already exist. Idempotent.

    A topic that is already present is reported as ``existing``, not created and
    not an error, so a re-deploy is a no-op. Creation is skipped entirely for a
    spec whose topic the broker already lists, which keeps the partition count
    of an existing topic untouched - widening partitions is a separate,
    deliberate operation, never a side effect of a deploy.
    """
    result = TopicEnsureResult()
    if not specs:
        return result

    if dry_run:
        result.created = [spec.name for spec in specs]
        return result

    try:
        admin = admin or build_admin(bootstrap=bootstrap, settings=settings)
        present = admin.list_topic_names()
    except Exception as exc:
        result.failed = [(spec.name, f"broker unreachable: {exc}") for spec in specs]
        return result

    for spec in specs:
        if spec.name in present:
            result.existing.append(spec.name)
            continue
        try:
            admin.create(
                spec.name,
                partitions=spec.partitions,
                replication_factor=spec.replication_factor,
            )
            result.created.append(spec.name)
            logger.info(
                f"Kafka topic created: {spec.name} "
                f"(partitions={spec.partitions}, rf={spec.replication_factor})"
            )
        except Exception as exc:
            result.failed.append((spec.name, str(exc)))
            logger.error(f"Kafka topic creation failed: {spec.name} - {exc}")

    return result
