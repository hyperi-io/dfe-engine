#  Project:      dfe-engine
#  File:         tests/hunt_runner/test_worker_identity.py
#  Purpose:      Per-pod unique lease-owner ids + settle resolution (no live CH)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Default worker ids MUST be unique per claimant, or never-double-run breaks.

The insert-and-resolve settle picks the winner as (latest claimed, then min
owner). If two pods share an owner id (the old fixed 'worker-0' default),
BOTH resolve as the winner and both run the hunt. These tests pin the derived
default id (hostname + pid + instance counter) and the exactly-one-winner
settle, using an in-memory hunt_lease emulation so no live ClickHouse is
needed (the live behaviour is covered by tests/integration/test_ch_coordinator.py).
"""

from __future__ import annotations

import itertools
import os
import socket
from types import SimpleNamespace

from dfe_engine.hunt_runner.ch_coordinator import ChCoordinator


class FakeLeaseCh:
    """Minimal in-memory hunt_lease table with the coordinator's read semantics.

    Emulates only what ChCoordinator's lease path touches: ``insert`` appends a
    row stamped with a monotonically increasing ``claimed`` sequence, and
    ``query`` answers the current-lease SELECT (ORDER BY claimed DESC, owner ASC
    LIMIT 1). ``freeze`` pins ``claimed`` so a same-instant tie is testable.
    """

    def __init__(self) -> None:
        self.rows: list[tuple[str, str, int, int, int]] = []
        self._seq = itertools.count()
        self._frozen: int | None = None

    def freeze(self) -> None:
        self._frozen = next(self._seq)

    def insert(self, table, rows, column_names=None, database=None) -> None:
        assert table == "hunt_lease"
        for hunt_id, owner, fire, lease_until in rows:
            claimed = self._frozen if self._frozen is not None else next(self._seq)
            self.rows.append((hunt_id, owner, int(fire), int(lease_until), claimed))

    def query(self, sql, parameters=None):
        assert "hunt_lease" in sql
        hunt_id = parameters["h"]
        matches = [r for r in self.rows if r[0] == hunt_id]
        matches.sort(key=lambda r: (-r[4], r[1]))  # claimed DESC, owner ASC
        return SimpleNamespace(result_rows=[(r[1], r[2], r[3]) for r in matches[:1]])

    def command(self, sql) -> None:  # ensure_schema is a no-op here
        pass


def _coord(fake: FakeLeaseCh, *, sleep=None) -> ChCoordinator:
    return ChCoordinator(
        fake,
        database="db",
        settle_seconds=0.0,
        clock=lambda: 100,
        sleep=sleep if sleep is not None else (lambda _s: None),
    )


def test_default_worker_ids_are_unique_per_coordinator():
    fake = FakeLeaseCh()
    c1 = ChCoordinator(fake, database="db")
    c2 = ChCoordinator(fake, database="db")
    # two claimants built via the default path MUST NOT share an owner id
    assert c1.worker_id != c2.worker_id
    # derived from hostname + pid so pods/processes are distinguishable
    assert socket.gethostname() in c1.worker_id
    assert str(os.getpid()) in c1.worker_id


def test_explicit_worker_id_still_wins():
    coord = ChCoordinator(FakeLeaseCh(), database="db", worker_id="w-explicit")
    assert coord.worker_id == "w-explicit"


def test_settle_with_two_distinct_owners_picks_exactly_one_winner():
    fake = FakeLeaseCh()
    w2 = _coord(fake)

    # w2's claim lands INSIDE w1's settle window - the race the pre-check
    # cannot see. The later claim wins deterministically.
    def race_sleep(_s: float) -> None:
        w2.renew("h", fire=1000, now=100)

    w1 = _coord(fake, sleep=race_sleep)

    assert w1.worker_id != w2.worker_id
    won = w1.try_claim("h", fire=1000, now=100)
    lease = w1.current_lease("h")
    assert lease is not None
    assert won is False  # w1 knows it lost the settle
    assert lease.owner == w2.worker_id  # exactly one winner: w2


def test_settle_tie_on_claimed_breaks_by_min_owner():
    fake = FakeLeaseCh()
    w1 = _coord(fake)
    w2 = _coord(fake)
    fake.freeze()  # both claims land at the same claimed instant
    w1.renew("h", fire=1000, now=100)
    w2.renew("h", fire=1000, now=100)
    lease = w1.current_lease("h")
    assert lease is not None
    assert lease.owner == min(w1.worker_id, w2.worker_id)
    # exactly one claimant resolves as the winner
    assert (lease.owner == w1.worker_id) != (lease.owner == w2.worker_id)
