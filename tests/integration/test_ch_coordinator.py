#  Project:      dfe-engine
#  File:         tests/integration/test_ch_coordinator.py
#  Purpose:      Adversarial tests of the ClickHouse hunt coordinator (real CH)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Live-ClickHouse tests of the insert-and-resolve claim + watermark + state.

No mocks: runs against the ClickHouse the .env configures, else a docker
ClickHouse (see conftest ch_client), in the ``dfe_db`` database that carries the
coordination tables the schema phase applies. Covers the guarantees the runner
leans on: claim exclusivity, lease expiry + reclaim, release, the cap input
(active_count), crash-safe watermark resume, and the too-aggressive overrun signal.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from dfe_engine.hunt_runner import ChCoordinator
from dfe_engine.hunt_runner.ch_coordinator import Lease


def _coord(ch_client, db, worker_id, *, now=None, lease_seconds=300):
    """A coordinator with a fixed clock and no-op settle sleep (deterministic)."""
    clock = (lambda: now) if now is not None else __import__("time").time
    return ChCoordinator(
        ch_client,
        database=db,
        worker_id=worker_id,
        lease_seconds=lease_seconds,
        settle_seconds=0.0,
        clock=clock,
        sleep=lambda _s: None,
    )


def test_claim_is_exclusive_between_workers(ch_client, dfe_db):
    w1 = _coord(ch_client, dfe_db, "w1")
    w2 = _coord(ch_client, dfe_db, "w2")
    w1.ensure_schema()

    assert w1.try_claim("h", fire=1000, now=100) is True
    # while w1's lease is active, a different worker cannot claim the same hunt
    assert w2.try_claim("h", fire=1000, now=105) is False
    # a re-claim by the SAME owner is idempotently true (still ours)
    assert w1.try_claim("h", fire=1000, now=106) is True


def test_lease_expiry_allows_reclaim(ch_client, dfe_db):
    w1 = _coord(ch_client, dfe_db, "w1")
    w2 = _coord(ch_client, dfe_db, "w2")
    w1.ensure_schema()

    assert (
        w1.try_claim(
            "h",
            fire=1000,
            now=100,
        )
        is True
    )  # lease_until default+100
    w1_short = _coord(ch_client, dfe_db, "w1", lease_seconds=10)
    assert w1_short.try_claim("h2", fire=1000, now=100) is True  # lease_until=110
    # still held before expiry
    assert w2.try_claim("h2", fire=1000, now=105) is False
    # after the lease expires, another worker reclaims it
    assert w2.try_claim("h2", fire=1000, now=200) is True


def test_release_frees_the_slot(ch_client, dfe_db):
    w1 = _coord(ch_client, dfe_db, "w1")
    w2 = _coord(ch_client, dfe_db, "w2")
    w1.ensure_schema()

    assert w1.try_claim("h", fire=1000, now=100) is True
    assert w1.active_count(now=105) == 1
    w1.release("h", fire=1000)
    assert w1.active_count(now=105) == 0
    # released -> a different worker can now claim immediately
    assert w2.try_claim("h", fire=1000, now=106) is True


def test_active_count_reflects_multiple_hunts(ch_client, dfe_db):
    w1 = _coord(ch_client, dfe_db, "w1")
    w1.ensure_schema()
    for i in range(3):
        assert w1.try_claim(f"h{i}", fire=1000, now=100) is True
    assert w1.active_count(now=105) == 3
    # a lease that has expired is not counted
    assert w1.active_count(now=10_000) == 0


def test_watermark_roundtrip_and_crash_resume(ch_client, dfe_db):
    w1 = _coord(ch_client, dfe_db, "w1")
    w1.ensure_schema()
    assert w1.get_watermark("h") is None
    w1.set_watermark("h", 1300)
    assert w1.get_watermark("h") == 1300
    # a fresh coordinator (crash/restart) still reads the committed watermark
    w1b = _coord(ch_client, dfe_db, "w1")
    assert w1b.get_watermark("h") == 1300
    # latest write wins
    w1.set_watermark("h", 1900)
    assert w1b.get_watermark("h") == 1900


def test_record_overrun_flags_too_aggressive(ch_client, dfe_db):
    w1 = _coord(ch_client, dfe_db, "w1")
    w1.ensure_schema()
    assert w1.get_state("h") is None
    w1.record_overrun("h")
    w1.record_overrun("h")
    state = w1.get_state("h")
    assert state is not None
    assert state.too_aggressive is True
    assert state.overrun_count == 2


def test_requires_explicit_database():
    with pytest.raises(ValueError):
        ChCoordinator(object(), database="")


class _PeerClaimsAfterThePrecheck(ChCoordinator):
    """A real coordinator whose peer claims the hunt between this one's lease read and insert.

    That gap is where two workers can both see a hunt free.
    """

    def __init__(self, *args, peer_claims: Callable[[], None], **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._peer_claims: Callable[[], None] | None = peer_claims

    def current_lease(self, hunt_id: str) -> Lease | None:
        lease = super().current_lease(hunt_id)
        hook, self._peer_claims = self._peer_claims, None
        if hook is not None:
            hook()
        return lease


def _late_claimant(ch_client, db: str, holder: ChCoordinator, won: list[bool]):
    """Worker b, which saw the hunt free just before *holder* claimed and won it."""
    return _PeerClaimsAfterThePrecheck(
        ch_client,
        database=db,
        worker_id="b",
        settle_seconds=0.0,
        sleep=lambda _s: None,
        peer_claims=lambda: won.append(holder.try_claim("h", fire=1000, now=1001)),
    )


def test_a_claim_inserted_after_the_holder_won_does_not_steal_the_hunt(ch_client, dfe_db):
    """a saw the hunt free and won; b saw it free too, but inserted after a had resolved."""
    a = _coord(ch_client, dfe_db, "a")
    a.ensure_schema()
    a_won: list[bool] = []
    b = _late_claimant(ch_client, dfe_db, a, a_won)

    assert b.try_claim("h", fire=1000, now=1001) is False
    assert a_won == [True]
    assert a.current_lease("h").owner == "a"


def test_a_refused_claim_leaves_nothing_that_holds_the_hunt(ch_client, dfe_db):
    """b's claim never landed, so once a releases the next claimant wins at once."""
    a = _coord(ch_client, dfe_db, "a")
    a.ensure_schema()
    a_won: list[bool] = []
    b = _late_claimant(ch_client, dfe_db, a, a_won)
    assert b.try_claim("h", fire=1000, now=1001) is False
    rows_by_b = ch_client.query(
        f"SELECT count() FROM `{dfe_db}`.hunt_lease WHERE hunt_id = 'h' AND owner = 'b'"
    ).result_rows[0][0]
    assert rows_by_b == 0

    a.release("h", fire=1000)
    c = _coord(ch_client, dfe_db, "c")
    assert c.try_claim("h", fire=1000, now=1002) is True
