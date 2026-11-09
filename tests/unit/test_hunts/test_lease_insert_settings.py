#  Project:      dfe-engine
#  File:         tests/unit/test_hunts/test_lease_insert_settings.py
#  Purpose:      Every lease write keeps identical rows, and two pods racing one claim
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""What the coordinator hands the driver for each lease write, and two pods racing a claim.

ClickHouse 26.3 never deduplicates an INSERT ... SELECT, so a real server cannot show
a lease write needing its settings. 24.8 does drop an identical one, so the settings
are asserted on what the driver is handed. The real-server sequences are in
``tests/integration/test_ch_coordinator.py``.

A real server cannot hold one claim's insert back while another claim settles, so the
race runs on a driver over an in-memory hunt_lease. Each insert is stamped when it
starts, as ``DEFAULT now64(3)`` is, and is seen only once it commits.
"""

import threading
from collections.abc import Callable
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from dfe_engine.hunts.ch_coordinator import ChCoordinator

KEEP = {"insert_deduplicate": 0, "deduplicate_insert": "disable"}

_SETTLE = 2.0

# A backstop on each hand-off between the two pod threads, never the thing waited for.
_HAND_OFF = 10.0


@dataclass(slots=True)
class _Row:
    hunt_id: str
    owner: str
    fire: int
    lease_until: int
    stamp: int
    seen: bool = False


class _Server:
    """One in-memory hunt_lease every pod's driver writes to."""

    def __init__(self, engine: str = "ReplicatedReplacingMergeTree", replicas: int | None = 3):
        self.engine = engine
        self.replicas = replicas
        self.rows: list[_Row] = []
        self._stamps = 0
        self._lock = threading.Lock()

    def begin(self, rows: list[list[Any]]) -> list[_Row]:
        """Stamp *rows* as one insert starting now, unseen until committed."""
        with self._lock:
            self._stamps += 1
            pending = [_Row(*row, stamp=self._stamps) for row in rows]
            self.rows.extend(pending)
        return pending

    def commit(self, pending: list[_Row]) -> None:
        with self._lock:
            for row in pending:
                row.seen = True

    def latest(self, key: str) -> _Row | None:
        """The row ``FINAL`` keeps for *key*: the newest committed stamp."""
        with self._lock:
            seen = [row for row in self.rows if row.seen and row.hunt_id == key]
        if not seen:
            return None
        return max(seen, key=lambda row: row.stamp)

    def final(self, key: str) -> _Row:
        """The row ``FINAL`` keeps for a *key* the test has written."""
        row = self.latest(key)
        assert row is not None, f"nothing committed under {key}"
        return row

    def keys(self, prefix: str) -> set[str]:
        with self._lock:
            return {row.hunt_id for row in self.rows if row.hunt_id.startswith(prefix)}


class _PodClock:
    """One pod's monotonic clock, which only its settle and its held insert move."""

    def __init__(self, on_settle: Callable[[], None] | None = None) -> None:
        self.now = 0.0
        self._on_settle = on_settle

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        hook, self._on_settle = self._on_settle, None
        if hook is not None:
            hook()


class _Driver:
    """A clickhouse-connect-shaped client for one pod over the shared :class:`_Server`.

    It answers a read by the parameters the coordinator binds rather than by parsing
    the SQL. An INSERT ... SELECT lands its rows without checking its condition, so it
    stands in only where that condition holds. *hold* runs once, between the first
    insert's start and its commit, and returns how long that insert took. *lag* is how
    long every plain insert takes.
    """

    def __init__(
        self,
        server: _Server,
        clock: _PodClock,
        hold: Callable[[], float] | None = None,
        lag: float = 0.0,
    ) -> None:
        self.server = server
        self.clock = clock
        self.writes: list[dict[str, Any]] = []
        self.system_reads: list[str] = []
        self.held_commit = threading.Event()
        self._hold = hold
        self.lag = lag

    def query(self, sql: str, parameters=None, **_kwargs) -> SimpleNamespace:
        params = parameters or {}
        for table in ("system.settings", "system.tables", "system.replicas"):
            if table in sql:
                self.system_reads.append(table)
                return SimpleNamespace(result_rows=self._system(table))
        if "o" in params:
            live = [
                key
                for key in self.server.keys(params["prefix"])
                if (row := self.server.latest(key)) is not None
                and row.owner != params["o"]
                and row.lease_until > params["now"]
            ]
            return SimpleNamespace(result_rows=[(len(live),)])
        if "m" in params:
            row = self.server.latest(params["m"])
            found = [] if row is None else [(row.fire, row.lease_until)]
            return SimpleNamespace(result_rows=found)
        row = self.server.latest(params["h"])
        if row is None:
            return SimpleNamespace(result_rows=[])
        # The row holds until its owner's marker for that fire ends or lapses.
        mark = self.server.latest(f"{params['prefix']}{row.owner}")
        if mark is None or mark.fire != row.fire:
            return SimpleNamespace(result_rows=[(row.owner, row.fire, 0)])
        until = min(row.lease_until, mark.lease_until)
        return SimpleNamespace(result_rows=[(row.owner, row.fire, until)])

    def _system(self, table: str) -> list[tuple[Any, ...]]:
        if table == "system.settings":
            return [(1,)]
        if table == "system.tables":
            return [(self.server.engine,)]
        return [] if self.server.replicas is None else [(self.server.replicas,)]

    def command(self, sql: str, parameters=None, settings=None, **_kwargs) -> None:
        params = parameters or {}
        until = params.get("u", 0)
        rows = [[params["h"], params["o"], params["f"], until]]
        if "m" in params:
            rows.append([params["m"], params["o"], params["f"], until])
        self._land(rows, settings)

    def insert(self, table: str, data, settings=None, **_kwargs) -> None:
        self.clock.now += self.lag
        self._land([list(row) for row in data], settings)

    def _land(self, rows: list[list[Any]], settings: dict[str, Any] | None) -> None:
        self.writes.append(dict(settings or {}))
        pending = self.server.begin(rows)
        hold, self._hold = self._hold, None
        if hold is not None:
            self.clock.now += hold()
        self.server.commit(pending)
        if hold is not None:
            self.held_commit.set()


def _pod(driver: _Driver, owner: str) -> ChCoordinator:
    return ChCoordinator(
        driver,
        database="dfe",
        worker_id=owner,
        settle_seconds=_SETTLE,
        sleep=driver.clock.sleep,
        monotonic=driver.clock.monotonic,
    )


_QUORUM = {"insert_quorum_parallel": 0, "insert_quorum_timeout": 10_000, "async_insert": 0}


@pytest.mark.parametrize(
    ("engine", "replicas", "quorum"),
    [
        pytest.param(
            "ReplicatedReplacingMergeTree", 3, {"insert_quorum": 2, **_QUORUM}, id="replicated"
        ),
        pytest.param("ReplacingMergeTree", None, {"insert_quorum": 1, **_QUORUM}, id="one-node"),
        pytest.param("SharedReplacingMergeTree", 3, {"async_insert": 0}, id="shared"),
    ],
)
def test_every_lease_write_keeps_identical_rows_and_waits_for_the_quorum(engine, replicas, quorum):
    """A re-claim repeats a row and has to land, and every write waits for the lease quorum."""
    driver = _Driver(_Server(engine, replicas), _PodClock())
    coord = _pod(driver, "w1")

    assert coord.try_claim("h", fire=1000, now=100) is True
    assert coord.renew("h", fire=1000, now=150) is True
    coord.release("h", fire=1000)
    assert coord.try_claim("h", fire=1000, now=200) is True
    coord.release("h", fire=1000)

    assert len(driver.writes) == 7
    for settings in driver.writes:
        assert KEEP.items() <= settings.items()
        assert quorum.items() <= settings.items()
    reads = driver.system_reads
    assert sorted(set(reads)) == sorted(reads), "the write settings were read more than once"


@pytest.mark.parametrize(
    ("b_commits", "winner"),
    [
        pytest.param("inside A's settle", "b", id="b-commits-inside-a-settle"),
        pytest.param("after A's settle", "a", id="b-commits-after-a-settle"),
    ],
)
def test_two_pods_that_both_read_a_hunt_free_never_both_win_it(b_commits, winner):
    """B reads the hunt free and starts its claim after A's, while A's is not yet committed.

    B's claim is the newer row either way. Committed inside A's settle, A's re-read sees
    it and B wins. Committed after, it landed a settle after B's free read, so it is not
    kept: its marker is ended and its row is left to lapse, never released over A.
    """
    server = _Server()
    a_started, b_started, a_settling, a_done = (threading.Event() for _ in range(4))
    inside = b_commits == "inside A's settle"

    def a_insert() -> float:
        a_started.set()
        assert b_started.wait(_HAND_OFF)
        return 0.0

    def b_insert() -> float:
        b_started.set()
        assert (a_settling if inside else a_done).wait(_HAND_OFF)
        return 0.01 if inside else _SETTLE

    driver_b = _Driver(server, _PodClock(), hold=b_insert)

    def a_settles() -> None:
        a_settling.set()
        assert driver_b.held_commit.wait(_HAND_OFF)

    driver_a = _Driver(server, _PodClock(on_settle=a_settles if inside else None), hold=a_insert)
    pod_a, pod_b = _pod(driver_a, "a"), _pod(driver_b, "b")
    b_won: list[bool] = []

    def b_claims() -> None:
        assert a_started.wait(_HAND_OFF)
        b_won.append(pod_b.try_claim("h", fire=1000, now=100))

    b_thread = threading.Thread(target=b_claims, daemon=True)
    b_thread.start()
    a_won = pod_a.try_claim("h", fire=1000, now=100)
    a_done.set()
    b_thread.join(_HAND_OFF)

    assert not b_thread.is_alive()
    assert (a_won, b_won) == (winner == "a", [winner == "b"])
    loser = "b" if winner == "a" else "a"
    assert server.final(f"h#claim/{winner}").lease_until == 400
    assert server.final(f"h#claim/{loser}").lease_until == 0
    assert server.final("h").owner == "b"
    assert all(row.lease_until for row in server.rows if row.hunt_id == "h")
    assert _pod(_Driver(server, _PodClock()), "c").try_claim("h", fire=1000, now=101) is False


@pytest.mark.parametrize(
    ("lease_seconds", "accepted"),
    [
        pytest.param(36, False, id="three-renewal-margins"),
        pytest.param(37, True, id="just-past-them"),
        pytest.param(300, True, id="default"),
    ],
)
def test_a_lease_too_short_to_renew_is_refused_at_construction(lease_seconds, accepted):
    """A renewal every third of the lease needs a settle plus the 10s quorum wait left."""
    driver = _Driver(_Server(), _PodClock())
    if accepted:
        ChCoordinator(driver, database="dfe", lease_seconds=lease_seconds, settle_seconds=_SETTLE)
        return
    with pytest.raises(ValueError, match="never renews"):
        ChCoordinator(driver, database="dfe", lease_seconds=lease_seconds, settle_seconds=_SETTLE)


@pytest.mark.parametrize("next_claimant", ["same-worker", "peer"])
def test_a_claim_dropped_for_landing_late_holds_nothing_on_the_next_poll(next_claimant):
    """A dropped claim's marker has ended, so its row frees the hunt at once, not after a lease."""
    server = _Server()
    late_driver = _Driver(server, _PodClock(), lag=_SETTLE)
    late = _pod(late_driver, "a")
    assert [late.try_claim("h", fire=1000, now=now) for now in (100, 150)] == [False, False]

    late_driver.lag = 0.0
    claimant = late if next_claimant == "same-worker" else _pod(_Driver(server, _PodClock()), "b")
    assert claimant.try_claim("h", fire=1000, now=151) is True
