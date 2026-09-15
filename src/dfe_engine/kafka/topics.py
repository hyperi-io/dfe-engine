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

Deleting a source removes the same pair. Left behind they cost a partition
assignment in every loader forever, and a record that still lands on one has no
table to go to.

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

from dfe_engine.source.models import transformed_topic

if TYPE_CHECKING:
    from dfe_engine.settings import DFESettings
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


@dataclass
class TopicRemoveResult:
    """Outcome of a remove pass: every requested topic lands in exactly one list."""

    removed: list[str] = field(default_factory=list)
    absent: list[str] = field(default_factory=list)
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
    settings: DFESettings | None = None,
) -> dict[str, Any]:
    """Build the librdkafka admin config, sourced from settings unless overridden.

    The provider DERIVES protocol and mechanism via the credential contract
    (dfe-engine#98); never hand-set the mechanism. A ``bootstrap`` pointing at a
    broker other than the configured one inherits no credentials - pass them
    alongside it, or the connection is made unauthenticated.
    """
    from dfe_engine.kafka import contract
    from dfe_engine.settings import get_settings

    ks = (settings or get_settings()).kafka

    # A credential belongs to the broker it was issued for. When the caller points
    # somewhere other than the configured broker and brings no credentials of its
    # own, the configured broker's password does NOT follow the address. Compare
    # the EFFECTIVE address: an empty override falls back and is not a redirect.
    bootstrap = bootstrap or ks.bootstrap_servers
    redirected = bootstrap != ks.bootstrap_servers
    unaccompanied = provider is None and username is None and password is None

    if redirected and unaccompanied:
        logger.warning(
            f"Kafka admin: bootstrap overridden to {bootstrap}, which is not the configured "
            "broker; connecting with no credentials rather than sending that broker's. "
            "Pass a provider and username/password for this address to authenticate."
        )
        protocol, mechanism = ("PLAINTEXT", "")
    else:
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
        # librdkafka accepts a mechanism on a non-SASL transport, warns to its own
        # stderr, and then connects UNAUTHENTICATED - configured-looking and open.
        # Refuse instead; ensure_topics reports it rather than deploying blind.
        if not protocol.upper().startswith("SASL"):
            raise contract.KafkaContractError(
                f"sasl.mechanism={mechanism!r} needs a SASL transport, got "
                f"security_protocol={protocol!r}; credentials would be silently dropped"
            )
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
    settings: DFESettings | None = None,
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
    version_id: str | None = None,
) -> list[TopicSpec]:
    """The topics one source needs: always ``_land``, plus ``_load`` if it transforms.

    ``version_id`` chooses which version answers "does it transform". A deploy MUST
    pass the version being deployed: ``Source.transform`` reads the version already
    deployed, so a release that ADDS a transform would be judged against the old
    one and its ``_load`` topic would never be created.
    """

    def _spec(name: str) -> TopicSpec:
        return TopicSpec(name=name, partitions=partitions, replication_factor=replication_factor)

    specs = [_spec(source.topic_land)]
    transform = source.version(version_id).transform if version_id else source.transform
    if transform:
        # Resolved against the CHOSEN version rather than whichever one happens
        # to be deployed, which is why this is not Source.topic_load.
        specs.append(_spec(transformed_topic(source.source)))
    return specs


def source_topic_names(source: Source) -> list[str]:
    """Every topic the engine ensured for this source, over all its versions.

    The inverse of ``source_topic_specs``, and it walks the versions rather than
    one of them: a version that added a transform had its ``_load`` topic created
    at its deploy, and a later version dropping the transform does not take the
    topic with it.
    """
    names = [source.topic_land]
    if any(version.transform for version in source.versions.values()):
        names.append(transformed_topic(source.source))
    return names


def ensure_topics(
    specs: list[TopicSpec],
    *,
    admin: TopicAdmin | None = None,
    settings: DFESettings | None = None,
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

    from dfe_engine.kafka.contract import KafkaContractError

    try:
        admin = admin or build_admin(bootstrap=bootstrap, settings=settings)
        present = admin.list_topic_names()
    except KafkaContractError as exc:
        # A rejected credential shape is a config fault, not an unreachable broker.
        # Reporting it as the latter sends the operator to the wrong place.
        result.failed = [(spec.name, f"kafka config rejected: {exc}") for spec in specs]
        return result
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


def remove_topics(
    names: list[str],
    *,
    admin: TopicAdmin | None = None,
    settings: DFESettings | None = None,
    bootstrap: str | None = None,
    dry_run: bool = False,
) -> TopicRemoveResult:
    """Delete every named topic that the broker still lists. Idempotent.

    DESTRUCTIVE: whatever is on the topic goes with it. Callers pass only the
    topics the engine itself created for a source it is deleting - a topic the
    broker does not list is reported as ``absent``, never as an error, so a
    re-run of a partly finished delete is a no-op.
    """
    result = TopicRemoveResult()
    if not names:
        return result

    if dry_run:
        result.removed = list(names)
        return result

    from dfe_engine.kafka.contract import KafkaContractError

    try:
        admin = admin or build_admin(bootstrap=bootstrap, settings=settings)
        present = admin.list_topic_names()
    except KafkaContractError as exc:
        result.failed = [(name, f"kafka config rejected: {exc}") for name in names]
        return result
    except Exception as exc:
        result.failed = [(name, f"broker unreachable: {exc}") for name in names]
        return result

    for name in names:
        if name not in present:
            result.absent.append(name)
            continue
        try:
            admin.delete(name)
            result.removed.append(name)
            logger.info(f"Kafka topic deleted: {name}")
        except Exception as exc:
            result.failed.append((name, str(exc)))
            logger.error(f"Kafka topic deletion failed: {name} - {exc}")

    return result
