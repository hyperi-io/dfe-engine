#  Project:      dfe-engine
#  File:         backing/observed.py
#  Purpose:      Read a managed backing service's size off the data plane
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""What the data plane reports a managed backing service's size to be.

Read through connections the engine already holds -- the broker admin client and
the ClickHouse connection -- so no provider control-plane credential is needed,
plus the ClickHouse Cloud service object where the deployment configured that
control plane. Every reader answers None for what it could not read: an observed
size is shown, never acted on, so a down broker or server blanks a field rather
than failing the request that asked.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from scalo.logger import logger

from dfe_engine.kafka.topics import read_broker_count

if TYPE_CHECKING:
    from dfe_engine.clickhouse.cloud import CloudServiceStatus
    from dfe_engine.settings import ClickHouseCloudSettings, DFESettings

DEFAULT_CLUSTER = "default"
"""The cluster ClickHouse Cloud and a stock server declare when no macro names one."""

# The cgroup limit is what a containerised server may use; the host total is the
# fallback where no cgroup limit is reported.
_MEMORY_METRICS = ("CGroupMemoryTotal", "OSMemoryTotal")


@dataclass(frozen=True, slots=True)
class ClickHouseTopology:
    """The ClickHouse cluster's shape, as the server the engine connects to reports it."""

    replicas: int | None = None
    """Replicas per shard in the cluster ``system.clusters`` lists."""

    shards: int | None = None
    """Shards in that cluster."""

    memory_bytes: int | None = None
    """Memory available to the server answering, from ``system.asynchronous_metrics``."""


def kafka_brokers(settings: DFESettings) -> int | None:
    """Brokers the bus's cluster metadata lists, or None when it cannot be read.

    Args:
        settings: The engine settings naming the broker and its credential.

    Returns:
        The broker count, or None on a deployment with no bus or a broker that
        did not answer.
    """
    if not settings.transport.bus_present:
        return None
    return read_broker_count(settings=settings)


def _cluster(client: Any) -> str | None:
    """The cluster to measure: the macro-declared one, else ``default`` where it exists."""
    rows = client.query("SELECT substitution FROM system.macros WHERE macro = 'cluster'")
    if rows.result_rows and rows.result_rows[0][0]:
        return str(rows.result_rows[0][0])
    present = client.query(
        "SELECT count() FROM system.clusters WHERE cluster = {cluster:String}",
        parameters={"cluster": DEFAULT_CLUSTER},
    )
    return DEFAULT_CLUSTER if present.result_rows and present.result_rows[0][0] else None


def _memory(client: Any) -> int | None:
    """The server's memory in bytes: its cgroup limit, else the host total."""
    rows = client.query(
        "SELECT metric, value FROM system.asynchronous_metrics "
        "WHERE has({names:Array(String)}, metric)",
        parameters={"names": list(_MEMORY_METRICS)},
    )
    reported = {str(metric): value for metric, value in rows.result_rows}
    for metric in _MEMORY_METRICS:
        value = reported.get(metric)
        if isinstance(value, int | float) and value > 0:
            return int(value)
    return None


def clickhouse_topology(connect: Callable[[], Any]) -> ClickHouseTopology:
    """The cluster's replicas and shards, and the answering server's memory.

    Replicas and shards stay None where the server declares no cluster to count:
    a lone server cannot say whether it is the only one.

    Args:
        connect: Returns a ClickHouse client whose ``query`` returns ``result_rows``.

    Returns:
        What could be read; every field None when the server did not answer.
    """
    # Any failure leaves the size unknown: the read is shown, never acted on.
    try:
        client = connect()
        replicas = shards = None
        cluster = _cluster(client)
        if cluster is not None:
            shape = client.query(
                "SELECT max(replica_num), uniqExact(shard_num) FROM system.clusters "
                "WHERE cluster = {cluster:String}",
                parameters={"cluster": cluster},
            ).result_rows
            if shape and shape[0][0]:
                replicas, shards = int(shape[0][0]), int(shape[0][1])
        return ClickHouseTopology(replicas=replicas, shards=shards, memory_bytes=_memory(client))
    except Exception as exc:
        logger.warning("ClickHouse size could not be read; reporting it unknown", error=str(exc))
        return ClickHouseTopology()


def cloud_service(config: ClickHouseCloudSettings, *, timeout: float) -> CloudServiceStatus | None:
    """The ClickHouse Cloud service object, where the deployment configured the control plane.

    One attempt within *timeout* per request, because a console waits on it.

    Args:
        config: The ``clickhouse.cloud`` settings.
        timeout: Seconds each management-API request may take.

    Returns:
        The service's status and stated sizing, or None when the control plane is
        not configured or did not answer.
    """
    from dfe_engine.clickhouse.cloud import CloudService, CloudServiceError

    if not config.configured:
        return None
    try:
        return CloudService(config, timeout=timeout, attempts=1).status()
    except CloudServiceError as exc:
        logger.warning("ClickHouse Cloud service object could not be read", error=str(exc))
        return None
