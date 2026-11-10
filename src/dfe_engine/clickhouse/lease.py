#  Project:      dfe-engine
#  File:         clickhouse/lease.py
#  Purpose:      The rules a claim on a ClickHouse lease table stands or falls by
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""How a claim on a ClickHouse lease table stands or falls.

A lease table keeps the latest write per key and has no row locks, so two claimers
that both read a key free both write a claim. Each claimer times its claim from the
start of its last free read to the insert's acknowledgement, waits one settle, then
re-reads. The claim stands only if it landed within that settle and is still the
latest row.

Two claims cannot both stand. A re-read that missed a rival's claim ran a settle after
its own claim landed, so the rival landed over a settle after the first claim. A rival
that stands began its free read under a settle before landing, so after the first claim
landed, and that read saw it. The bound is tight: a claimer only knows its own timing,
and a claim taking a settle or more from its last free read can land after a rival's
re-read, so landing late cannot be allowed for, only retried.

Every lease write reaches a majority of the table's replicas before ClickHouse
acknowledges it, and every lease read answers only from a replica holding all of them,
so a re-read on a lagging replica still sees the claim that beat it. A claim that
misses is never released, because it may be the latest row over a rival that stood.

The schema lock (:mod:`dfe_engine.schema.lock`) and the hunt coordinator
(:mod:`dfe_engine.hunts.ch_coordinator`) both claim this way, each on its own table.
"""

import time
from collections.abc import Callable
from typing import Any

from dfe_engine.clickhouse.errors import parse_code

# A claim stands only if acknowledged within this long of its free read, so every rival sees it.
# A quorum write to a second replica takes up to about a second, so this clears it twice over.
SETTLE_SECONDS = 2.0

# A lease read answers only from a replica holding every claim a quorum acknowledged.
READ_SETTINGS = {"select_sequential_consistency": 1}

# A claim acknowledged after its lease lapsed could be taken over, so this ends well inside a lease.
QUORUM_TIMEOUT_MS = 10_000

# A missed quorum, which a later attempt can clear: TOO_FEW_LIVE_REPLICAS,
# UNSATISFIED_QUORUM_FOR_PREVIOUS_WRITE, REPLICA_IS_NOT_IN_QUORUM, UNKNOWN_STATUS_OF_INSERT.
_QUORUM_REFUSALS = frozenset({285, 286, 289, 319})

# REPLICA_IS_NOT_IN_QUORUM: this replica lacks the part Keeper records as the last quorum write.
_MISSING_QUORUM_PART = 289


class SlowClaimError(Exception):
    """A claim acknowledged a settle or more after its free read, so it is not kept.

    Attributes:
        landed: Seconds from the free read to the acknowledgement.
    """

    def __init__(self, landed: float) -> None:
        super().__init__(f"the claim landed {landed:.2f}s after its free read")
        self.landed = landed


def is_quorum_refusal(exc: BaseException) -> bool:
    """Whether *exc* is ClickHouse refusing a missed quorum, which a later attempt can clear."""
    return parse_code(exc) in _QUORUM_REFUSALS


def is_missing_quorum_part(exc: BaseException) -> bool:
    """Whether *exc* refuses a sequential read for want of the last quorum part.

    A lagging replica says this until it fetches the part. So does every replica once
    the part is gone, after TTL empties it, and that lasts until the next quorum write.
    """
    return parse_code(exc) == _MISSING_QUORUM_PART


def majority(replicas: int) -> int:
    """A majority of *replicas*: 1 on one node, which ClickHouse reads as no quorum wait."""
    return max(replicas, 1) // 2 + 1


def write_settings_for(engine: str, replicas: int) -> dict[str, int]:
    """What a lease write carries on a table of *engine* with *replicas* replicas.

    Every SharedMergeTree insert is already a quorum insert and ClickHouse documents
    the quorum settings as not applicable to it, so they are left off there.
    ``insert_quorum = 'auto'`` never resolves on a single replica and refuses every
    later write behind it, so elsewhere the majority is counted. ClickHouse refuses a
    sequential quorum on an async insert, so every write is synchronous.
    """
    if engine.startswith("Shared"):
        return {"async_insert": 0}
    return {
        "insert_quorum": majority(replicas),
        "insert_quorum_parallel": 0,
        "insert_quorum_timeout": QUORUM_TIMEOUT_MS,
        "async_insert": 0,
    }


def write_settings(client: Any, *, database: str, table: str) -> dict[str, int]:
    """The settings :func:`write_settings_for` picks for *table* as it stands on the server.

    Args:
        client: A clickhouse-connect client or the engine's wrapper of one.
        database: The database holding the lease table.
        table: The lease table's name.

    Raises:
        Exception: Whatever the driver raises when the engine or replica count cannot be read.
    """
    parameters = {"db": database, "tbl": table}
    engines = client.query(
        "SELECT engine FROM system.tables WHERE database = {db:String} AND name = {tbl:String}",
        parameters=parameters,
    ).result_rows
    engine = str(engines[0][0]) if engines else ""
    if engine.startswith("Shared"):
        return write_settings_for(engine, 0)
    replicas = client.query(
        "SELECT total_replicas FROM system.replicas "
        "WHERE database = {db:String} AND table = {tbl:String}",
        parameters=parameters,
    ).result_rows
    return write_settings_for(engine, int(replicas[0][0]) if replicas else 1)


def settle_claim(
    started: float,
    *,
    stands: Callable[[], bool],
    end: Callable[[], None],
    settle: float = SETTLE_SECONDS,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Keep a claim just acknowledged only if it landed in time and is still the latest.

    Args:
        started: The *monotonic* reading taken before the claim's free read.
        stands: Whether the latest claim is still this one.
        end: Mark this claim as holding nothing, without releasing its row.
        settle: How soon a claim must land, and how long it waits before the re-read.
        monotonic: The clock *started* was read from.
        sleep: How the settle is waited out.

    Returns:
        Whether the claim stands. One that lost has been ended.

    Raises:
        SlowClaimError: The claim landed a settle or more after *started*, so a rival's
            re-read may have missed it. It has been ended and lapses with its lease.
    """
    landed = monotonic() - started
    if landed >= settle:
        end()
        raise SlowClaimError(landed)
    sleep(settle)
    if stands():
        return True
    end()
    return False
