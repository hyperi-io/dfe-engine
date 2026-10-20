#  Project:      dfe-engine
#  File:         schema/phase.py
#  Purpose:      The boot phase that makes every manifest object exist
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The schema bootstrap phase: the engine is the only thing that applies DDL.

It runs inside the API lifespan, ahead of readiness, and in one order: resolve
the schema tree, take the lease, render the plan, compare it against the ledger
and the live catalogue, apply what is additive, refuse what is not, record every
object, release the lease, create the declared topics, and confirm the dead-letter
topics are on the broker.

Two things make it a GATE rather than the best-effort pass it replaces. A failure
leaves the engine UP and NotReady, with the stage that failed on the status route
and the cause in the engine log, because a silent failure and a success look
identical to every app downstream. And
ClickHouse or the broker merely not being up yet is not a failure: the phase
retries for a bounded window first, which is what a stack whose datastore starts
in the same wave needs.
"""

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from dfe_schemas.clickhouse import ReplicationNotConfiguredError
from scalo.logger import logger

from dfe_engine import __version__ as engine_version
from dfe_engine.clickhouse.errors import is_retryable_error, parse_code
from dfe_engine.schema.ledger import LedgerError, MigrationLedger
from dfe_engine.schema.lock import SchemaLock, SchemaLockError, holder_name, lease_seconds
from dfe_engine.schema.manifest_applier import (
    ManifestApplier,
    ManifestApplyError,
    ManifestReport,
    log_report,
)
from dfe_engine.schema.plan import LEDGER_ID, LOCK_ID, SchemaPlan, SchemaPlanError, build_plan
from dfe_engine.startup_stop import stoppable_wait

if TYPE_CHECKING:
    from dfe_engine.kafka.topics import TopicAdmin, TopicSpec
    from dfe_engine.schema.metrics import SchemaMetrics
    from dfe_engine.settings import DFESettings

DEAD_LETTER_TOPIC_KIND = "dlq"
"""The dfe-schemas topic kind every app's dead-letter topic is declared under."""


class DeadLetterPathError(Exception):
    """A declared dead-letter topic is not on the broker, so a dead letter would be lost."""


# What the deployment is told about the schema. `running` is what the status
# route reports while the phase is mid-pass, so a slow apply reads as in
# progress rather than as broken.
STATE_UNKNOWN = "unknown"
STATE_CONVERGED = "converged"
STATE_FAILED = "failed"
STATE_RUNNING = "running"
STATE_OBSERVED = "observed"

_STATE_GAUGE = {
    STATE_UNKNOWN: 0,
    STATE_CONVERGED: 1,
    STATE_FAILED: 2,
    STATE_RUNNING: 3,
    STATE_OBSERVED: 4,
}

# The state is served to every signed-in caller, so a backend's own error text stays
# in the log and the state names only the stage that failed.
_FAILED_STAGES: tuple[tuple[type[Exception], str], ...] = (
    (ManifestApplyError, "ClickHouse refused or did not answer the manifest apply"),
    (SchemaPlanError, "the schema manifest could not be planned"),
    (SchemaLockError, "the schema lock could not be read or written"),
    (LedgerError, "the migration ledger could not be read or written"),
)
_SEE_LOG = "the engine log has the cause"


def _failed_stage(exc: Exception) -> str:
    """The status route's account of a failed pass: the stage, never the backend's text.

    A server refused for replication is the exception: dfe-schemas wrote that
    message, and it names what the operator has to add.
    """
    if isinstance(exc, ReplicationNotConfiguredError):
        return str(exc)
    for kind, stage in _FAILED_STAGES:
        if isinstance(exc, kind):
            return f"{stage}; {_SEE_LOG}"
    return f"unexpected failure; {_SEE_LOG}"


_MEMORY_LIMIT_EXCEEDED = 241


def is_transient_failure(exc: BaseException) -> bool:
    """Whether a later pass can clear *exc*: ClickHouse not answering, or refusing for load.

    Each stage wraps the driver's error, so the whole cause chain is read. The query
    retry rule never retries 241, because the same query meets the same ceiling at
    once; a pass runs again only after a back-off, and its writes are small, so the
    server's memory pressure may have passed by then.

    Args:
        exc: What failed the pass.

    Returns:
        True when any error in the chain is a connection outage, a rate limit or 241.
    """
    seen: set[int] = set()
    link: BaseException | None = exc
    while link is not None and id(link) not in seen:
        seen.add(id(link))
        if is_retryable_error(link) or parse_code(link) == _MEMORY_LIMIT_EXCEEDED:
            return True
        link = link.__cause__ or link.__context__
    return False


@dataclass
class SchemaBootstrapState:
    """What the last pass did, for the status route, the metrics and readiness."""

    state: str = STATE_UNKNOWN
    schemas_version: str = ""
    engine_version: str = engine_version
    platform: str = ""
    database: str = ""
    holder: str = ""
    started_at: str = ""
    finished_at: str = ""
    duration_seconds: float = 0.0
    error: str = ""
    objects: list[dict[str, Any]] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)
    overlay_refused: list[str] = field(default_factory=list)
    topics_created: list[str] = field(default_factory=list)
    topics_skipped: str = ""
    # Its own field rather than ``refused``: that list means ClickHouse objects
    # an operator clears with ``dfe schema apply --allow-drift``, which reaches
    # no topic, and a non-empty ``refused`` exits the CLI 2.
    topics_drift: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    # Not on the status route: it decides whether the daemon runs the pass again.
    transient: bool = False
    """A later pass can clear what stopped this one.

    ClickHouse not answering or refusing for load, or another applier's lease.
    """

    @property
    def converged(self) -> bool:
        """Whether the manifest's objects are known to exist on this deployment.

        ``observed`` does not count: the lease's holder was still applying when
        this pass stopped waiting, so what it was applying may not exist yet.
        """
        return self.state == STATE_CONVERGED

    @property
    def ready(self) -> bool:
        """Whether readiness may be reported. A refusal is drift, not a failed pass.

        ``unknown`` is ready: the phase was switched off, and off means the engine
        reports the schema state as unknown and GATES nothing rather than holding
        the deployment down over a dial an operator set deliberately. ``observed``
        is not, until a later pass converges.
        """
        return self.state not in (STATE_FAILED, STATE_RUNNING, STATE_OBSERVED)

    @property
    def gauge(self) -> int:
        """The numeric state ``dfe_schema_bootstrap_state`` carries."""
        return _STATE_GAUGE.get(self.state, 0)

    def as_dict(self) -> dict[str, Any]:
        """The status route's payload."""
        return {
            "state": self.state,
            "ready": self.ready,
            "converged": self.converged,
            "schemas_version": self.schemas_version,
            "engine_version": self.engine_version,
            "platform": self.platform,
            "database": self.database,
            "holder": self.holder,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": round(self.duration_seconds, 3),
            "error": self.error,
            "counts": dict(self.counts),
            "objects": list(self.objects),
            "refused": list(self.refused),
            "overlay_refused": list(self.overlay_refused),
            "topics_created": list(self.topics_created),
            "topics_skipped": self.topics_skipped,
            "topics_drift": list(self.topics_drift),
        }


_state = SchemaBootstrapState()


def current_state() -> SchemaBootstrapState:
    """The last pass's state. Unknown until the phase has run in this process."""
    return _state


def schema_ready() -> bool:
    """The readiness check ``/readyz`` registers. Never consulted by ``/livez``.

    False where the pass FAILED, is still running, or gave up waiting on another
    applier's lease, so a pod with a broken or unapplied schema stays up and
    NotReady with the cause on the status route.
    """
    return _state.ready


def _set_state(state: SchemaBootstrapState) -> None:
    # One process-wide reading, read by the status route and the readiness probe.
    global _state
    _state = state


def _connect(settings: DFESettings, *, wait_seconds: float) -> Any:
    """A live ClickHouse client, retrying for a bounded window.

    ClickHouse and the engine can be in the same deploy wave, so "not up yet" and
    "broken" are different answers and only the second one should fail the phase.
    """
    from dfe_engine.clickhouse.clickhouse_manager import ClickHouseManager
    from dfe_engine.settings import get_clickhouse_config

    deadline = time.monotonic() + max(wait_seconds, 0.0)
    # Nothing is written until this returns, so a stop may end it here.
    with stoppable_wait():
        while True:
            try:
                manager = ClickHouseManager.get_instance(get_clickhouse_config(settings=settings))
                client = manager.get_clickhouse_client()
                client.command("SELECT 1")
                return client
            except Exception as exc:
                if time.monotonic() >= deadline:
                    raise ManifestApplyError(f"ClickHouse did not answer: {exc}") from exc
                logger.info("waiting for ClickHouse before the schema apply", error=str(exc))
                time.sleep(min(5.0, max(wait_seconds / 10.0, 1.0)))


def apply_plan(
    client: Any,
    plan: SchemaPlan,
    *,
    dry_run: bool = False,
    allow_drift: bool = False,
) -> ManifestReport:
    """Apply every ClickHouse object in the plan, in manifest order.

    The ledger and the lease table are ordinary manifest objects, and the
    manifest declares them first, so they are created by this same loop before
    anything is recorded into them.
    """
    applier = ManifestApplier(
        client,
        ledger=None,
        schemas_version=plan.schemas_version,
        engine_version=engine_version,
        platform=plan.platform,
        dry_run=dry_run,
        allow_drift=allow_drift,
    )
    ledger_object = plan.by_id(LEDGER_ID)
    ledger = MigrationLedger(
        client, database=ledger_object.database or plan.data_database, table=ledger_object.name
    )

    objects = plan.tables()
    # Up to and including the ledger first, so the compare that follows has a
    # table to read; the ledger's own row is written by the same pass.
    head = next(i for i, obj in enumerate(objects) if obj.id == LEDGER_ID) + 1
    for rendered in objects[:head]:
        applier.apply(rendered)

    if not dry_run:
        try:
            applier.attach_ledger(ledger)
        except LedgerError as exc:
            logger.warning(
                "the migration ledger could not be read; every object is treated as new",
                error=str(exc),
            )

    _flush_query_log(client, dry_run=dry_run)
    for rendered in objects[head:]:
        applier.apply(rendered)

    applier.flush()
    return applier.report


def _flush_query_log(client: Any, *, dry_run: bool) -> None:
    """Materialise ``system.query_log`` before the cost archive's view is applied.

    ClickHouse creates that table on the first log flush, so on a freshly started
    server the view's source does not exist yet. Non-fatal: a server with query
    logging disabled never materialises it at all, and the archive is declared
    optional for exactly that reason.
    """
    if dry_run:
        return
    from dfe_engine.clickhouse import query_log_archive

    try:
        query_log_archive.flush_logs(client)
    except Exception as exc:
        logger.debug("system.query_log could not be flushed", error=str(exc))


def _broker_count(settings: DFESettings) -> int:
    """Brokers to clamp the topic set's replication factor against.

    Only asked where the topic set is actually going to be created, so a
    deployment with no bus gains no broker round trip.
    """
    if _topics_skipped(settings):
        return 1
    from dfe_engine.kafka.topics import broker_count

    return broker_count(settings=settings)


def _topics_skipped(settings: DFESettings) -> str:
    """Why the bootstrap topic set will not be created, or "" when it will be.

    One reader for the gate, because the plan is rendered for a broker count read
    off the bus and that read must not happen on a deployment the apply is going
    to skip -- a brokerless tier would spend the admin timeout on every boot.
    """
    if not settings.kafka.bootstrap_topics:
        return "the topic bootstrap is switched off"
    if not settings.transport.bus_present:
        return "this deployment carries no bus"
    if not settings.kafka.bootstrap_servers:
        return "no broker is configured"
    return ""


def _bootstrap_specs(
    plan: SchemaPlan, settings: DFESettings, *, kind: str | None = None
) -> list[TopicSpec]:
    """The declared bootstrap topics, of one kind or all, at the deployment's message size.

    The manifest renders its own size, and a managed broker capped below it refuses
    the create, which holds the engine NotReady on the dead-letter set.
    """
    from dfe_engine.kafka.topics import TopicSpec, topic_max_message_bytes

    size = str(topic_max_message_bytes(settings))
    return [
        TopicSpec(
            name=rendered.topic["name"],
            partitions=int(rendered.topic["partitions"]),
            replication_factor=int(rendered.topic["replication_factor"]),
            config={**rendered.topic["config"], "max.message.bytes": size},
        )
        for rendered in plan.topics()
        if rendered.topic and (kind is None or rendered.topic.get("kind") == kind)
    ]


def _apply_topics(plan: SchemaPlan, settings: DFESettings) -> tuple[list[str], str, list[str]]:
    """Create the declared bootstrap topics, and report any whose shape differs.

    A deployment with no bus is not a failure: the brokerless tiers run the
    receiver straight into the loader over gRPC, so an absent broker means the
    topic set is skipped and the pass still converges.

    ``ensure_topics`` leaves an existing topic untouched, so a topic created by
    anything else keeps its own partitions, retention and replication factor. The
    dry-run converge pass names that difference instead of adopting it silently;
    applying it stays a separate, deliberate operation.
    """
    from dfe_engine.kafka.topics import ensure_topics, update_topics

    skipped = _topics_skipped(settings)
    if skipped:
        return [], skipped, []

    specs = _bootstrap_specs(plan, settings)
    result = ensure_topics(specs, settings=settings)
    for name, error in result.failed:
        logger.warning("bootstrap topic not created", topic=name, error=error)

    drift: list[str] = []
    if result.existing:
        existing_specs = [s for s in specs if s.name in set(result.existing)]
        try:
            compared = update_topics(existing_specs, settings=settings, dry_run=True)
        except Exception as exc:  # a describe fault must not fail the create pass
            logger.warning("bootstrap topics not compared", error=str(exc))
        else:
            drift = [f"{name}: {reason}" for name, reason in compared.refused]
            drift += [f"{n}: config differs from the manifest" for n in compared.altered]
            drift += [f"{n}: fewer partitions than the manifest asks for" for n in compared.widened]
            # A topic the broker would not describe is UNKNOWN, not clean: without
            # this a principal missing DescribeConfigs reports a converged
            # deployment with no drift, which is what #439 exists to surface.
            drift += [
                f"{n}: shape unreadable, so drift is unknown -- {r}" for n, r in compared.failed
            ]
            for entry in drift:
                logger.warning("bootstrap topic differs from the manifest", detail=entry)
    return result.created, "", drift


def require_dead_letter_topics(
    plan: SchemaPlan,
    settings: DFESettings,
    *,
    wait_seconds: float,
    admin: TopicAdmin | None = None,
) -> list[str]:
    """Confirm every declared dead-letter topic is on the broker, or refuse the pass.

    A deployment that cannot record a dead letter discards it silently, so it is
    held NotReady instead of running. Retried for the same bounded window as the
    ClickHouse connect, since the broker can start in the same wave. A deployment
    with no bus, or whose topic bootstrap is off, records no dead letter on Kafka
    through this set and is not held to it.

    Returns the dead-letter topics confirmed, which is empty where none apply.

    Raises:
        DeadLetterPathError: A dead-letter topic is still not on the broker when
            the window closes.
    """
    from dfe_engine.kafka.topics import ensure_topics

    if _topics_skipped(settings):
        return []
    specs = _bootstrap_specs(plan, settings, kind=DEAD_LETTER_TOPIC_KIND)
    if not specs:
        return []
    deadline = time.monotonic() + max(wait_seconds, 0.0)
    # A topic create is all-or-nothing and the next start repeats it, so a stop may end it here.
    with stoppable_wait():
        while True:
            result = ensure_topics(specs, admin=admin, settings=settings)
            if result.ok:
                return sorted(result.created + result.existing)
            if time.monotonic() >= deadline:
                missing = "; ".join(f"{name}: {error}" for name, error in result.failed)
                raise DeadLetterPathError(
                    f"dead-letter topic(s) are not on the broker, so a dead letter would be "
                    f"lost: {missing}"
                )
            logger.info(
                "waiting for the broker to hold the dead-letter topics",
                missing=[name for name, _ in result.failed],
            )
            time.sleep(min(5.0, max(wait_seconds / 10.0, 1.0)))


def run_bootstrap(
    *,
    settings: DFESettings,
    wait_seconds: float | None = None,
    allow_drift: bool = False,
    metrics: SchemaMetrics | None = None,
) -> SchemaBootstrapState:
    """Bring this deployment's ClickHouse and topic set to the pinned manifest.

    Returns the state, which is also what the status route and the readiness
    check read. Never raises: a failure is reported as state ``failed`` naming the
    stage that failed, with the cause in the engine log, so the pod stays up for an
    operator to find it.

    Args:
        settings: The deployment's settings.
        wait_seconds: How long to wait for ClickHouse and the lease; ``None``
            takes ``clickhouse.bootstrap_wait_seconds``.
        allow_drift: Also apply the column type and TTL changes a pass refuses as drift.
        metrics: Where the pass reports its outcome. ``None`` reports nothing.
    """
    ch = settings.clickhouse
    started = datetime.now(UTC)
    state = SchemaBootstrapState(
        state=STATE_RUNNING,
        database=ch.effective_data_database,
        holder=holder_name(),
        started_at=started.isoformat(),
    )
    _set_state(state)

    if not ch.bootstrap_tables:
        state.state = STATE_UNKNOWN
        state.error = "DFE_CLICKHOUSE_BOOTSTRAP_TABLES is off; the schema state is not known here"
        logger.warning(state.error)
        _finish(state, started, metrics)
        return state

    wait = ch.bootstrap_wait_seconds if wait_seconds is None else wait_seconds
    try:
        client = _connect(settings, wait_seconds=wait)
        plan = build_plan(
            settings=settings,
            client=client,
            broker_count=_broker_count(settings),
            kafka_tiered_storage=settings.kafka.tiered_storage,
        )
        state.schemas_version = plan.schemas_version
        state.platform = plan.platform
        state.overlay_refused = list(plan.refused)

        lock_object = plan.by_id(LOCK_ID)
        lock = SchemaLock(
            client,
            database=lock_object.database or plan.data_database,
            table=lock_object.name,
            lease_seconds=lease_seconds(wait),
        )
        state.holder = lock.holder
        report = _apply_under_lock(
            client, plan, lock, wait=wait, allow_drift=allow_drift, state=state
        )
    except (
        ManifestApplyError,
        SchemaPlanError,
        SchemaLockError,
        LedgerError,
        ReplicationNotConfiguredError,
    ) as exc:
        state.state = STATE_FAILED
        state.error = _failed_stage(exc)
        state.transient = is_transient_failure(exc)
        logger.error("schema bootstrap failed; the engine stays up and NotReady", error=str(exc))
        _finish(state, started, metrics)
        return state
    except Exception as exc:
        state.state = STATE_FAILED
        state.error = _failed_stage(exc)
        state.transient = is_transient_failure(exc)
        logger.exception("schema bootstrap failed; the engine stays up and NotReady")
        _finish(state, started, metrics)
        return state

    # The holder creates the topics too, so a pass that applied nothing leaves them to it.
    if state.state == STATE_OBSERVED:
        _finish(state, started, metrics)
        return state

    if report is not None:
        log_report(report, prefix="schema bootstrap")
        state.counts = report.counts()
        state.objects = [
            {
                "id": outcome.id,
                "kind": outcome.kind,
                "object": outcome.qualified,
                "action": outcome.action,
                "checksum": outcome.checksum,
                "columns_added": list(outcome.columns_added),
                "drift": list(outcome.drift),
                "extra_columns": list(outcome.extra_columns),
            }
            for outcome in report.outcomes
        ]
        state.refused = [f"{o.qualified}: {'; '.join(o.drift) or o.reason}" for o in report.refused]

    try:
        state.topics_created, state.topics_skipped, state.topics_drift = _apply_topics(
            plan, settings
        )
    except Exception as exc:  # a broker fault must not take the ClickHouse apply with it
        state.topics_skipped = f"topic bootstrap failed; {_SEE_LOG}"
        logger.warning("bootstrap topics not created", error=str(exc))

    try:
        require_dead_letter_topics(plan, settings, wait_seconds=wait)
    except DeadLetterPathError as exc:
        state.state = STATE_FAILED
        state.error = str(exc)
        logger.error(
            "dead letters cannot be recorded; the engine stays up and NotReady", error=str(exc)
        )
        _finish(state, started, metrics)
        return state

    state.state = STATE_CONVERGED
    _finish(state, started, metrics)
    return state


def _apply_under_lock(
    client: Any,
    plan: SchemaPlan,
    lock: SchemaLock,
    *,
    wait: float,
    allow_drift: bool,
    state: SchemaBootstrapState,
) -> ManifestReport | None:
    """Take the lease and apply, waiting for a live holder to finish first.

    The lease table is itself a manifest object, so it has to exist before it can
    be read. A server that does not carry it yet is a first boot, and this pass
    is the one that creates it.

    A pass whose wait runs out while the holder is still live applies nothing and
    is marked ``observed`` and transient: the holder may still be mid-apply, so
    readiness waits for a later pass that takes the lease itself.
    """
    if _table_absent(lock):
        return apply_plan(client, plan, allow_drift=allow_drift)

    with lock:
        if not lock.acquire(wait_seconds=wait):
            held = lock.read()
            state.state = STATE_OBSERVED
            state.holder = held.holder if held is not None else ""
            state.error = "another applier held the schema lease for the whole wait"
            state.transient = True
            logger.warning(
                "schema lease held elsewhere for the whole wait; NotReady until a later pass",
                holder=state.holder,
            )
            return None
        return apply_plan(client, plan, allow_drift=allow_drift)


def _table_absent(lock: SchemaLock) -> bool:
    """Whether the lease table is missing, which is what a first boot looks like."""
    try:
        return not lock.table_exists()
    except SchemaLockError:
        return True


def _finish(state: SchemaBootstrapState, started: datetime, metrics: SchemaMetrics | None) -> None:
    finished = datetime.now(UTC)
    state.finished_at = finished.isoformat()
    state.duration_seconds = (finished - started).total_seconds()
    _set_state(state)
    if metrics is not None:
        _report_metrics(state, metrics)


def _report_metrics(state: SchemaBootstrapState, metrics: SchemaMetrics) -> None:
    """Push the pass's outcome. A metrics backend fault is not a failure."""
    try:
        metrics.report(
            state=state.gauge,
            duration_seconds=state.duration_seconds,
            schemas_version=state.schemas_version,
            refused=len(state.refused),
        )
    except Exception as exc:
        logger.debug("schema bootstrap metrics not reported", error=str(exc))
