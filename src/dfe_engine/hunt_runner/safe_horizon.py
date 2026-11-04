#  Project:      dfe-engine
#  File:         hunt_runner/safe_horizon.py
#  Purpose:      Read the safe horizon a hunt fire must not read past
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The safe horizon: the newest ``_timestamp_load`` every replica already holds.

A hunt never reads past it. A row whose load time is before the fire but whose part
has not reached the replica answering the hunt is otherwise never matched, because
the watermark moves past it. ClickHouse reports how far a replica is behind in
``system.replicas.absolute_delay``, which needs no grant beyond reading the table,
so the horizon is the server's clock less the largest delay over the hunt's source
and detection tables, less a commit allowance for an insert not yet in the queue.

A table nothing is replicating into has no delay, so a quiet table never holds a
fire, and a server with no replicated table reports none at all.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from dfe_engine.clickhouse.quoting import quote_literal

# The coordinator's per-hunt marks: when the hunt was first held, and where its next
# fire re-reads from after the cap let one run short of the horizon.
DEFERRED_SINCE = "deferred_since"
SAFE_FROM = "safe_from"


@dataclass(frozen=True, slots=True)
class SafeHorizon:
    """What the replica answering a hunt holds, read once per fire.

    Attributes:
        now: The server's clock, epoch seconds.
        delay_seconds: The largest replication delay over the hunt's tables.
        commit_allowance_seconds: How far the horizon trails the delay once there is one.
        target_delay_seconds: The largest delay over the hunt's detection tables alone,
            where the dedupe looks for what it already detected.
    """

    now: int
    delay_seconds: int
    commit_allowance_seconds: int
    target_delay_seconds: int = 0

    @property
    def horizon(self) -> int:
        """Return the newest load time, epoch seconds, every replica already holds."""
        if self.delay_seconds <= 0:
            return self.now
        return self.now - self.delay_seconds - self.commit_allowance_seconds

    def seconds_behind(self, fire: int) -> int:
        """Return how far the horizon is short of *fire*, 0 when it has reached it.

        A replica with no delay holds every committed row, so it is never behind, even
        for a fire the server's clock has not reached.
        """
        if self.delay_seconds <= 0:
            return 0
        return max(0, fire - self.horizon)


def _listed(pairs: Iterable[tuple[str, str]]) -> str:
    return ", ".join(f"({quote_literal(db)}, {quote_literal(table)})" for db, table in pairs)


def safe_horizon_sql(
    tables: Iterable[tuple[str, str]], targets: Iterable[tuple[str, str]] = ()
) -> str:
    """The query reading the server's clock, the largest delay over *tables*, and over *targets*.

    Raises:
        ValueError: If *tables* is empty, since the read would measure nothing.
    """
    pairs = sorted(set(tables))
    if not pairs:
        raise ValueError("a safe horizon over no table measures nothing")
    target_pairs = sorted(set(targets))
    target_delay = "0"
    if target_pairs:
        target_delay = (
            f"toInt64(maxIf(absolute_delay, (database, table) IN ({_listed(target_pairs)})))"
        )
    return (
        f"SELECT toInt64(now()), toInt64(max(absolute_delay)), {target_delay} "  # noqa: S608 - quoted literals
        f"FROM system.replicas WHERE (database, table) IN ({_listed(pairs)})"
    )


def read_safe_horizon(
    ch: Any,
    tables: Iterable[tuple[str, str]],
    *,
    commit_allowance_seconds: int,
    targets: Iterable[tuple[str, str]] = (),
) -> SafeHorizon:
    """Read the safe horizon for *tables* on the server *ch* answers from.

    Args:
        ch: A clickhouse-connect client.
        tables: The (database, table) pairs the fire reads and writes.
        commit_allowance_seconds: How far the horizon trails a non-zero delay.
        targets: The detection tables among *tables*, whose delay is also read alone.

    Returns:
        The reading. A table with no replica row counts as no delay.
    """
    # An aggregate with no GROUP BY returns one row, with a delay of 0 over no replica row.
    now, delay, target_delay = ch.query(safe_horizon_sql(tables, targets)).result_rows[0]
    return SafeHorizon(
        now=int(now),
        delay_seconds=int(delay or 0),
        commit_allowance_seconds=commit_allowance_seconds,
        target_delay_seconds=int(target_delay or 0),
    )
