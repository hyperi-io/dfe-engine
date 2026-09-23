#  Project:      dfe-engine
#  File:         kafka/topics.py
#  Purpose:      Explicit Kafka topic admin - the topics a Source needs, created
#                by DFE rather than left to broker auto-create. dfe-engine#97.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Topic admin shared by the API, the Helm compiler and the source deploy hook.

DFE creates the topics its sources need EXPLICITLY. Relying on the broker's
``auto.create.topics.enable`` silently produces mis-partitioned, unmanaged
topics on first produce, which is the anti-pattern ``dfe kafka topics`` exists
to replace - so nothing in the engine may depend on it.

The whole contract lives here: ensure on deploy, converge (config alter and
partition widening) on update, status and drift for a read, remove on delete,
and one pass over every enabled source at startup. A partition DECREASE is
refused rather than attempted - Kafka has no such operation, and the records
already assigned to the partitions it would drop have nowhere to go.

Every source needs ``<label>_land`` (raw, receiver -> Kafka); a source with a
transform also needs ``<source>_load`` (transformed, transform -> loader). That
``_land``/``_load`` convention is shared with scalo-rs and computed by
``Source.topic_land`` / ``Source.topic_load``.

A source landing on the shared topic is the exception to both: ``Source.owns_landing_topic``
is false for it, so it neither creates nor deletes a ``_land`` topic - the landing
source owns that one.

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

from confluent_kafka.admin import (
    AdminClient,
    AlterConfigOpType,
    ConfigEntry,
    ConfigResource,
    NewPartitions,
    NewTopic,
)
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
    # Alterable topic configs (retention.ms, cleanup.policy). Empty means "leave
    # the broker default alone", which is what every topic created before these
    # dials existed already carries.
    config: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "partitions": self.partitions,
            "replication_factor": self.replication_factor,
            "config": dict(self.config),
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


@dataclass
class TopicUpdateResult:
    """Outcome of a converge pass: what was altered, widened, refused or failed.

    ``refused`` is separate from ``failed`` on purpose: a partition decrease and a
    replication-factor change are things DFE declines to do, not things the broker
    rejected, and an operator reading a deploy response needs to tell them apart.
    """

    altered: list[str] = field(default_factory=list)
    widened: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    absent: list[str] = field(default_factory=list)
    refused: list[tuple[str, str]] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when the broker rejected nothing; a refusal is not a failure."""
        return not self.failed


@dataclass
class TopicState:
    """What the broker holds for one topic, against what the source asks for."""

    name: str
    source: str
    exists: bool
    desired_partitions: int
    desired_replication_factor: int
    partitions: int | None = None
    replication_factor: int | None = None
    config: dict[str, str] = field(default_factory=dict)
    drift: list[str] = field(default_factory=list)


@dataclass
class TopicStatusResult:
    """Per-topic status for a status read; ``error`` is set when the broker is not reachable."""

    topics: list[TopicState] = field(default_factory=list)
    error: str | None = None

    @property
    def reachable(self) -> bool:
        """True when the broker answered; the topic list is only meaningful then."""
        return self.error is None


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

    def node_count(self, *, timeout: float = 10.0) -> int:
        """Brokers the cluster reports, for clamping a topic's replication factor."""
        description = self._admin.describe_cluster(request_timeout=timeout).result()
        return len(description.nodes)

    def create(
        self,
        name: str,
        *,
        partitions: int,
        replication_factor: int,
        config: dict[str, str] | None = None,
        timeout: float = 30.0,
    ) -> None:
        new_topic = NewTopic(
            name,
            num_partitions=partitions,
            replication_factor=replication_factor,
            config=dict(config or {}),
        )
        futures = self._admin.create_topics([new_topic], request_timeout=timeout)
        futures[name].result()

    def delete(self, name: str, *, timeout: float = 30.0) -> None:
        futures = self._admin.delete_topics([name], request_timeout=timeout)
        futures[name].result()

    def describe_shape(self, name: str, *, timeout: float = 10.0) -> tuple[int, int]:
        """``(partitions, replication_factor)`` for one existing topic.

        Read off cluster metadata rather than describe_topics so it works on every
        broker the contract covers; the replication factor is the replica count of
        the topic's first partition, which is what a uniform topic has.
        """
        metadata = self._admin.list_topics(topic=name, timeout=timeout)
        partitions = metadata.topics[name].partitions
        replicas = next(iter(partitions.values())).replicas if partitions else []
        return len(partitions), len(replicas)

    def describe_config(self, name: str, *, timeout: float = 30.0) -> dict[str, str]:
        """Every topic-level config the broker reports, as strings."""
        resource = ConfigResource(ConfigResource.Type.TOPIC, name)
        futures = self._admin.describe_configs([resource], request_timeout=timeout)
        entries = futures[resource].result()
        return {
            key: "" if entry.value is None else str(entry.value) for key, entry in entries.items()
        }

    def alter_config(self, name: str, changes: dict[str, str], *, timeout: float = 30.0) -> None:
        """Incrementally SET the named configs, leaving every other one untouched.

        Incremental, not a whole-resource alter: the non-incremental call resets
        anything it is not given back to the broker default, which would silently
        undo an operator's own tuning on a topic DFE only wanted to converge.
        """
        resource = ConfigResource(ConfigResource.Type.TOPIC, name)
        for key, value in changes.items():
            resource.add_incremental_config(
                ConfigEntry(key, value, incremental_operation=AlterConfigOpType.SET)
            )
        futures = self._admin.incremental_alter_configs([resource], request_timeout=timeout)
        futures[resource].result()

    def widen_partitions(self, name: str, *, total: int, timeout: float = 30.0) -> None:
        """Raise the topic's partition count to ``total``. Kafka cannot lower it."""
        futures = self._admin.create_partitions(
            [NewPartitions(name, total)], request_timeout=timeout
        )
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
    config: dict[str, str] | None = None,
    version_id: str | None = None,
) -> list[TopicSpec]:
    """The topics one source needs: always ``_land``, plus ``_load`` if it transforms.

    ``version_id`` chooses which version answers "does it transform". A deploy MUST
    pass the version being deployed: ``Source.transform`` reads the version already
    deployed, so a release that ADDS a transform would be judged against the old
    one and its ``_load`` topic would never be created.
    """

    def _spec(name: str) -> TopicSpec:
        return TopicSpec(
            name=name,
            partitions=partitions,
            replication_factor=replication_factor,
            config=dict(config or {}),
        )

    # A source landing on the shared topic shares one the landing source already
    # owns, so it contributes no spec of its own rather than claiming that topic
    # and its partition settings.
    specs = [_spec(source.topic_land)] if source.owns_landing_topic else []
    transform = source.version(version_id).transform if version_id else source.transform
    if transform:
        # Resolved against the CHOSEN version rather than whichever one happens
        # to be deployed, which is why this is not Source.topic_load.
        specs.append(_spec(transformed_topic(source.source)))
    return specs


def topics_managed(settings: DFESettings) -> bool:
    """Whether this deployment manages its sources' topics at all.

    Whether a bus is present is the FACT; ``kafka.ensure_topics`` is the operator's
    override on top of it, and unset follows the fact. One reader so the deploy
    hook, the delete hook, the API and the startup pass cannot disagree.
    """
    override = settings.kafka.ensure_topics
    return settings.transport.bus_present if override is None else bool(override)


def topics_managed_at_startup(settings: DFESettings) -> bool:
    """Whether the boot-time pass runs. A stricter test than ``topics_managed``.

    It reaches the broker before anything has asked for a topic, so it runs only
    where an operator explicitly turned topic management on. Left unset it does
    not: a deployment that merely carries a bus may not have told the engine
    where the broker is, and every boot would spend the admin timeout finding
    that out. The chart sets the dial whenever a broker address is configured.
    """
    return settings.kafka.ensure_topics is True


def broker_count(
    *,
    settings: DFESettings | None = None,
    admin: TopicAdmin | None = None,
) -> int:
    """Brokers the configured bus reports, or 1 when it cannot be asked.

    The replication clamp needs the bus's real size, and only the bus knows it --
    the configured factor compared against itself never reduces anything.

    1 on any fault, never the configured factor: a topic created asking for more
    replicas than the cluster can place is accepted and then never becomes Ready,
    which is a worse failure than running one replica.
    """
    from dfe_engine.kafka.contract import KafkaContractError

    try:
        admin = admin or build_admin(settings=settings)
        nodes = admin.node_count()
    except KafkaContractError as exc:
        logger.warning("kafka config rejected; clamping replication to one broker", error=str(exc))
        return 1
    except Exception as exc:
        logger.warning("broker unreachable; clamping replication to one broker", error=str(exc))
        return 1
    if nodes < 1:
        logger.warning("the bus reported no brokers; clamping replication to one")
        return 1
    logger.info("kafka broker count read for the replication clamp", brokers=nodes)
    return nodes


def deployment_topic_config(settings: DFESettings) -> dict[str, str]:
    """The alterable topic configs DFE asks for, as librdkafka string values.

    Empty by default: a deployment that sets neither dial gets a topic whose
    retention and cleanup policy are the broker's, which is what every topic
    created before these dials existed already has.
    """
    ks = settings.kafka
    config: dict[str, str] = {}
    if ks.topic_retention_ms is not None:
        config["retention.ms"] = str(ks.topic_retention_ms)
    if ks.topic_cleanup_policy:
        config["cleanup.policy"] = ks.topic_cleanup_policy
    return config


def specs_for_sources(
    sources: list[Source], settings: DFESettings
) -> tuple[list[TopicSpec], dict[str, str]]:
    """Every source's topic specs, de-duplicated, plus the source that owns each.

    Sources share a topic only by name collision, which the registry already
    refuses, so the first owner wins and the duplicate is dropped rather than
    ensured twice.
    """
    ks = settings.kafka
    config = deployment_topic_config(settings)
    specs: list[TopicSpec] = []
    owners: dict[str, str] = {}
    for source in sources:
        for spec in source_topic_specs(
            source,
            partitions=ks.topic_partitions,
            replication_factor=ks.topic_replication_factor,
            config=config,
        ):
            if not spec.name or spec.name in owners:
                continue
            owners[spec.name] = source.source
            specs.append(spec)
    return specs, owners


def ensure_all_source_topics(
    sources: list[Source],
    settings: DFESettings,
    *,
    admin: TopicAdmin | None = None,
) -> TopicEnsureResult:
    """One ensure pass over every source this deployment carries.

    Runs at startup so a fresh broker holds the topics before anything produces
    into them; without it a deployment restored from its config repo has topics
    only for the sources somebody happens to redeploy.
    """
    if not topics_managed_at_startup(settings):
        return TopicEnsureResult()
    specs, _ = specs_for_sources(sources, settings)
    return ensure_topics(specs, admin=admin, settings=settings)


def source_topic_names(source: Source) -> list[str]:
    """Every topic the engine ensured for this source, over all its versions.

    The inverse of ``source_topic_specs``, and it walks the versions rather than
    one of them: a version that added a transform had its ``_load`` topic created
    at its deploy, and a later version dropping the transform does not take the
    topic with it.
    """
    # Never the shared landing topic: removing one source that lands on it would
    # take the topic out from under the receiver's default flow and every other
    # source landing there.
    names = [source.topic_land] if source.owns_landing_topic else []
    if any(version.transform for version in source.versions.values()):
        names.append(transformed_topic(source.source))
    return names


def stranded_source_topics(
    source: Source,
    *,
    version_id: str | None = None,
    settings: DFESettings | None = None,
    admin: TopicAdmin | None = None,
) -> list[str]:
    """Topics on the broker that the version being deployed has no consumer for.

    A version that dropped its transform leaves its ``_load`` topic behind, and
    scalo's resolver suppresses a ``_land`` topic whenever the matching ``_load``
    one exists -- so the loader stops reading the topic the receiver is still
    producing to, and the source silently stops loading.

    Reported rather than deleted: records may still be in flight on it, and
    whether the engine may remove a topic mid-life is not this function's call.
    """
    transform = source.version(version_id).transform if version_id else source.transform
    if transform:
        return []
    load_topic = transformed_topic(source.source)
    try:
        admin = admin or build_admin(settings=settings)
        present = admin.list_topic_names()
    except Exception:
        return []
    return [load_topic] if load_topic in present else []


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
                config=spec.config,
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


def _config_drift(spec: TopicSpec, actual: dict[str, str]) -> dict[str, str]:
    """The configs the spec asks for whose broker value differs. Only what is asked.

    Everything the operator set outside the spec is left out, so a converge never
    proposes to undo tuning the source definition says nothing about.
    """
    return {key: value for key, value in spec.config.items() if actual.get(key) != value}


def topic_status(
    specs: list[TopicSpec],
    *,
    sources: dict[str, str] | None = None,
    admin: TopicAdmin | None = None,
    settings: DFESettings | None = None,
    bootstrap: str | None = None,
) -> TopicStatusResult:
    """Report what the broker holds for each spec, and how it differs.

    Read-only: nothing here creates, alters or deletes. A broker that cannot be
    reached sets ``error`` and returns no topics, rather than reporting every
    topic as missing - "absent" and "could not look" are different answers.
    ``sources`` maps a topic name to the source that owns it, for the response.
    """
    result = TopicStatusResult()
    if not specs:
        return result

    from dfe_engine.kafka.contract import KafkaContractError

    try:
        admin = admin or build_admin(bootstrap=bootstrap, settings=settings)
        present = admin.list_topic_names()
    except KafkaContractError as exc:
        result.error = f"kafka config rejected: {exc}"
        return result
    except Exception as exc:
        result.error = f"broker unreachable: {exc}"
        return result

    owners = sources or {}
    for spec in specs:
        state = TopicState(
            name=spec.name,
            source=owners.get(spec.name, ""),
            exists=spec.name in present,
            desired_partitions=spec.partitions,
            desired_replication_factor=spec.replication_factor,
        )
        if not state.exists:
            state.drift.append("topic does not exist")
            result.topics.append(state)
            continue
        try:
            state.partitions, state.replication_factor = admin.describe_shape(spec.name)
            state.config = admin.describe_config(spec.name)
        except Exception as exc:
            state.drift.append(f"could not be described: {exc}")
            result.topics.append(state)
            continue

        if state.partitions is not None and state.partitions != spec.partitions:
            state.drift.append(f"partitions {state.partitions}, wanted {spec.partitions}")
        if (
            state.replication_factor is not None
            and state.replication_factor != spec.replication_factor
        ):
            state.drift.append(
                f"replication factor {state.replication_factor}, wanted {spec.replication_factor}"
            )
        for key, value in _config_drift(spec, state.config).items():
            state.drift.append(f"{key} {state.config.get(key, '(unset)')}, wanted {value}")
        result.topics.append(state)

    return result


def update_topics(
    specs: list[TopicSpec],
    *,
    admin: TopicAdmin | None = None,
    settings: DFESettings | None = None,
    bootstrap: str | None = None,
    dry_run: bool = False,
) -> TopicUpdateResult:
    """Converge every existing topic onto its spec. Idempotent.

    Two changes are applied: an incremental alter of the configs the spec names,
    and a partition increase. Three are REFUSED and reported instead - a partition
    decrease (Kafka has no such operation), a replication-factor change (a
    reassignment, not a topic alter), and a topic that does not exist (that is
    ``ensure_topics``, and creating it here would hide a missed deploy).
    """
    result = TopicUpdateResult()
    if not specs:
        return result

    from dfe_engine.kafka.contract import KafkaContractError

    try:
        admin = admin or build_admin(bootstrap=bootstrap, settings=settings)
        present = admin.list_topic_names()
    except KafkaContractError as exc:
        result.failed = [(spec.name, f"kafka config rejected: {exc}") for spec in specs]
        return result
    except Exception as exc:
        result.failed = [(spec.name, f"broker unreachable: {exc}") for spec in specs]
        return result

    for spec in specs:
        if spec.name not in present:
            result.absent.append(spec.name)
            continue
        try:
            partitions, replication_factor = admin.describe_shape(spec.name)
            # A spec asking for no configs cannot drift on any, so the round trip
            # that would find that out is skipped.
            drifted = _config_drift(spec, admin.describe_config(spec.name)) if spec.config else {}
        except Exception as exc:
            result.failed.append((spec.name, str(exc)))
            logger.error(f"Kafka topic could not be described: {spec.name} - {exc}")
            continue

        if replication_factor != spec.replication_factor:
            result.refused.append(
                (
                    spec.name,
                    f"replication factor is {replication_factor}, not {spec.replication_factor}: "
                    "changing it is a partition reassignment, not a topic alter",
                )
            )
        if partitions > spec.partitions:
            result.refused.append(
                (
                    spec.name,
                    f"has {partitions} partitions, more than the {spec.partitions} asked for: "
                    "Kafka cannot drop a partition, and the records on it have nowhere to go",
                )
            )

        changed = False
        if drifted and not dry_run:
            try:
                admin.alter_config(spec.name, drifted)
            except Exception as exc:
                result.failed.append((spec.name, str(exc)))
                logger.error(f"Kafka topic config alter failed: {spec.name} - {exc}")
                continue
            logger.info(f"Kafka topic config altered: {spec.name} ({sorted(drifted)})")
        if drifted:
            result.altered.append(spec.name)
            changed = True

        if partitions < spec.partitions:
            if not dry_run:
                try:
                    admin.widen_partitions(spec.name, total=spec.partitions)
                except Exception as exc:
                    result.failed.append((spec.name, str(exc)))
                    logger.error(f"Kafka topic partition widening failed: {spec.name} - {exc}")
                    continue
                logger.info(
                    f"Kafka topic widened: {spec.name} ({partitions} -> {spec.partitions} partitions)"
                )
            result.widened.append(spec.name)
            changed = True

        if not changed:
            result.unchanged.append(spec.name)

    return result
