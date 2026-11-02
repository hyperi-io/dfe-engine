#  Project:      dfe-engine
#  File:         tests/integration/test_schema/test_manifest_bootstrap.py
#  Purpose:      Rule 1 -- the engine alone brings an empty ClickHouse to the manifest
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Start from an empty ClickHouse and end with every manifest object, recorded.

Against a REAL server, because that is the only thing that answers the question
the rule asks. A mock would agree with whatever the applier believes, and the
failures this replaces -- a table on one replica, an engine clause the server
rejects, a create that silently no-ops -- are all server behaviour.

Proven here:

* one pass over an empty database creates every ClickHouse object the manifest
  declares, and nothing else creates any of them
* every applied object is recorded in ``schema_migrations`` with the checksum of
  the statement that ran, and an identical record still lands
* a second pass changes nothing, which is what makes the phase safe to run on
  every boot
* the schema lease leaves one holder when claimers wait, race, tie to the
  millisecond, or release after their lease was taken over
* every lease read and write carries the settings that make a claim visible on
  every replica once it is acknowledged, so claimers on two replicas, or on two
  replicas that cannot fetch from each other, never both hold
* a claim that cannot reach its quorum is not taken, and does not raise
* a claim that lands slower than the settle does not stand, and is released
* a lease release that fails after the apply keeps the apply's result
"""

import secrets
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import clickhouse_connect
import pytest
from dfe_schemas.clickhouse import Platform, create_database

from dfe_engine.schema import lock as lock_module
from dfe_engine.schema import phase
from dfe_engine.schema.ledger import MigrationLedger
from dfe_engine.schema.lock import SchemaLock, SchemaLockError, holder_name
from dfe_engine.schema.phase import apply_plan
from dfe_engine.schema.plan import LEDGER_ID, LOCK_ID, build_plan, render_one

pytestmark = [pytest.mark.integration, pytest.mark.own_clickhouse]

# Kinds that land in the data or meta database and are visible in system.tables.
_TABLE_KINDS = ("table", "materialized_view", "view")

# A claim held up by a server stall is a missed attempt, so a caller that must take the lease waits.
_RIDE_OUT = 10.0


@pytest.fixture
def empty_database(ch_client):
    """A database name that does not exist yet, dropped afterwards."""
    name = f"dfe_manifest_{uuid.uuid4().hex[:8]}"
    yield name
    try:
        ch_client.command(f"DROP DATABASE IF EXISTS `{name}` SYNC")
    except Exception:
        pass


@pytest.fixture
def plan(empty_database):
    """The full manifest plan, rendered against the throwaway database."""
    from dfe_engine.settings import load_settings

    settings = load_settings().model_copy(deep=True)
    settings.clickhouse.data_database = empty_database
    return build_plan(settings=settings)


def _live_tables(client, database: str) -> set[str]:
    rows = client.query(
        "SELECT name FROM system.tables WHERE database = {db:String}",
        parameters={"db": database},
    ).result_rows
    return {str(row[0]) for row in rows}


def _live_databases(client) -> set[str]:
    return {str(row[0]) for row in client.query("SELECT name FROM system.databases").result_rows}


def _live_roles(client) -> set[str]:
    return {str(row[0]) for row in client.query("SELECT name FROM system.roles").result_rows}


def test_bootstrap_from_empty_clickhouse_creates_every_manifest_object(
    ch_client, empty_database, plan
):
    """Rule 1: one engine pass is the whole of what a deployment's schema needs."""
    assert empty_database not in _live_databases(ch_client)

    report = apply_plan(ch_client, plan)

    assert not report.refused, [outcome.describe() for outcome in report.refused]
    counts = report.counts()
    assert counts["created"] > 0
    assert counts["unchanged"] == 0

    databases = _live_databases(ch_client)
    roles = _live_roles(ch_client)
    for rendered in plan.tables():
        if rendered.kind == "database":
            assert rendered.name in databases, f"{rendered.id} was not created"
        elif rendered.kind == "role":
            # The rendered name is the catalogue's entry; the object carries the
            # naming rule's role form.
            assert any(rendered.name in role for role in roles), f"{rendered.id} was not created"

    applied = {
        rendered.name
        for rendered in plan.tables()
        if rendered.kind in _TABLE_KINDS and rendered.database == empty_database
    }
    skipped = {outcome.name for outcome in report.outcomes if outcome.action == "skipped"}
    missing = applied - _live_tables(ch_client, empty_database) - skipped
    assert not missing, f"the manifest declares these and the pass did not create them: {missing}"


def test_every_applied_object_is_recorded_with_its_checksum(ch_client, empty_database, plan):
    """The ledger answers which dfe-schemas release each object came from."""
    apply_plan(ch_client, plan)

    ledger_object = plan.by_id(LEDGER_ID)
    recorded = MigrationLedger(
        ch_client, database=empty_database, table=ledger_object.name
    ).checksums()

    for rendered in plan.tables():
        key = (rendered.database or "", rendered.name)
        row = recorded.get(key)
        if row is None:
            continue
        assert row.checksum == rendered.checksum, f"{rendered.id} recorded a stale checksum"
        assert row.schemas_version == plan.schemas_version
        assert row.platform == plan.platform

    assert (empty_database, ledger_object.name) in recorded, "the ledger records itself"


def test_two_identical_ledger_records_both_land(ch_client, empty_database, plan):
    """An unchanged pass writes the rows the last one wrote, and applied_at must move on."""
    apply_plan(ch_client, plan)
    ledger_object = plan.by_id(LEDGER_ID)
    ledger = MigrationLedger(ch_client, database=empty_database, table=ledger_object.name)
    row = MigrationLedger.row(
        database=empty_database,
        name="probe_object",
        kind="table",
        schemas_version="0.0.0",
        engine_version="0.0.0",
        checksum="same",
        statement="CREATE TABLE probe_object",
        action="created",
        platform="self_hosted",
    )

    def applied_at():
        return ch_client.query(
            f"SELECT max(applied_at) FROM `{empty_database}`.`{ledger_object.name}` "
            "WHERE object = 'probe_object'"
        ).result_rows[0][0]

    ledger.record([row])
    first = applied_at()
    time.sleep(0.05)
    ledger.record([row])

    # Read as the latest version, so a merge folding the two rows cannot hide the answer.
    assert applied_at() > first, "the second identical record was dropped as a duplicate"


def test_a_second_pass_changes_nothing(ch_client, empty_database, plan):
    """Idempotent by construction, which is what makes it a boot phase."""
    apply_plan(ch_client, plan)
    second = apply_plan(ch_client, plan)

    changed = [
        outcome.describe()
        for outcome in second.outcomes
        if outcome.action in ("created", "altered")
    ]
    assert not changed, changed
    assert not second.refused, [outcome.describe() for outcome in second.refused]


def test_a_second_holder_waits_and_reports_rather_than_applying(ch_client, empty_database, plan):
    """Rule 3 of the phase: one applier per stack, through the lease."""
    apply_plan(ch_client, plan)
    lock_object = plan.by_id(LOCK_ID)

    first = SchemaLock(
        ch_client, database=empty_database, table=lock_object.name, holder="pod-a", lease_seconds=60
    )
    second = SchemaLock(
        ch_client, database=empty_database, table=lock_object.name, holder="pod-b", lease_seconds=60
    )

    assert first.acquire(wait_seconds=_RIDE_OUT) is True
    assert second.acquire(wait_seconds=0.0) is False
    first.release()
    assert second.acquire(wait_seconds=_RIDE_OUT) is True
    second.release()


class _FirstClaimMisses(SchemaLock):
    """A lock whose first claim lands too slowly to stand, or lands and then reports a missed quorum."""

    def __init__(self, *args, miss: str, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._miss = miss

    def _claim(self, now: datetime) -> datetime:
        claimed = super()._claim(now)
        miss, self._miss = self._miss, "none"
        if miss == "slow":
            time.sleep(lock_module._SETTLE_SECONDS)
        elif miss == "quorum":
            raise lock_module.SchemaLockQuorumError(
                "Code: 319. DB::Exception: UNKNOWN_STATUS_OF_INSERT"
            )
        return claimed


@pytest.mark.parametrize("miss", ["none", "slow", "quorum"])
def test_appliers_that_find_the_lease_free_together_leave_one_holder(
    ch_params, ch_client, empty_database, plan, miss
):
    """Claims written in the same instant leave one holder, even when every racer's first claim misses.

    A missed claim never stood, so it must not keep the others waiting out a lease.
    """
    apply_plan(ch_client, plan)
    table = plan.by_id(LOCK_ID).name
    racers = 4
    start = threading.Barrier(racers, timeout=60)
    outcomes: list[bool | Exception] = []

    def race(holder: str) -> None:
        try:
            client = clickhouse_connect.get_client(**ch_params)
            lock = _FirstClaimMisses(
                client,
                database=empty_database,
                table=table,
                holder=holder,
                lease_seconds=60,
                miss=miss,
            )
            start.wait()
            outcomes.append(lock.acquire(wait_seconds=_RIDE_OUT))
        except Exception as exc:  # a racer that raises must fail the test, not drop out of it
            start.abort()
            outcomes.append(exc)

    threads = [threading.Thread(target=race, args=(f"pod-{n}",)) for n in range(racers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    errors = [outcome for outcome in outcomes if isinstance(outcome, Exception)]
    assert not errors, errors
    assert len(outcomes) == racers
    assert outcomes.count(True) == 1, outcomes


def test_claims_in_the_same_millisecond_keep_one_holder_through_a_merge(
    ch_params, ch_client, empty_database, plan
):
    """Two claimers on one pod at one instant: one stands, both name it, and a merge keeps it."""
    apply_plan(ch_client, plan)
    table = plan.by_id(LOCK_ID).name
    target = f"`{empty_database}`.`{table}`"
    locks = [
        SchemaLock(
            clickhouse_connect.get_client(**ch_params),
            database=empty_database,
            table=table,
            lease_seconds=60,
        )
        for _ in range(2)
    ]
    # The larger holder writes first, so a merge keeps the claim a holder-ordered tie-break loses.
    locks.sort(key=lambda lock: lock.holder, reverse=True)
    holders = [lock.holder for lock in locks]
    assert holders[0] != holders[1]
    assert all(holder.startswith(f"{holder_name()}/") for holder in holders), holders

    # Private calls: no public path can make two claims share one timestamp.
    now = datetime.now(UTC)
    claimed = [lock._claim(now) for lock in locks]
    assert claimed[0] == claimed[1]

    def outcome() -> tuple[list[bool], list[str | None]]:
        stands = [lock._claim_stands(at) for lock, at in zip(locks, claimed, strict=True)]
        seen = [lock.read() for lock in locks]
        return stands, [state.holder if state is not None else None for state in seen]

    def stored_rows() -> int:
        # The lease rows alone: each claim's marker sits under a lock name of its own.
        return ch_client.query(
            f"SELECT count() FROM {target} WHERE lock_name = {{lock:String}}",
            parameters={"lock": lock_module.LOCK_NAME},
        ).result_rows[0][0]

    assert stored_rows() == 2, "the tie merged before it could be read unmerged"
    stands, named = outcome()
    assert stands.count(True) == 1, (holders, stands)
    winner = holders[stands.index(True)]
    assert named == [winner, winner]

    ch_client.command(f"OPTIMIZE TABLE {target} FINAL")

    assert stored_rows() == 1, "OPTIMIZE did not fold the tie"
    assert outcome() == (stands, [winner, winner])


def test_a_release_after_the_lease_was_taken_over_leaves_the_takeover_held(
    ch_params, ch_client, empty_database, plan
):
    """A holder that ran past its expiry and then releases must not free its successor's lease."""
    apply_plan(ch_client, plan)
    table = plan.by_id(LOCK_ID).name

    def lock(holder: str, lease_seconds: int) -> SchemaLock:
        return SchemaLock(
            clickhouse_connect.get_client(**ch_params),
            database=empty_database,
            table=table,
            holder=holder,
            lease_seconds=lease_seconds,
        )

    stale = lock("pod-h", 1)
    assert stale.acquire(wait_seconds=_RIDE_OUT) is True
    time.sleep(1.5)
    takeover = lock("pod-n", 60)
    assert takeover.acquire(wait_seconds=_RIDE_OUT) is True, "the lapsed lease was not taken over"

    stale.release()

    latest = takeover.read()
    assert latest is not None
    assert (latest.holder, latest.released) == ("pod-n", False)
    assert lock("pod-x", 60).acquire(wait_seconds=0) is False


def _lease_queries(client, database: str, table: str) -> list[tuple[str, dict[str, str]]]:
    """Every finished SELECT and INSERT this server ran against the lease table, with its settings."""
    client.command("SYSTEM FLUSH LOGS")
    rows = client.query(
        "SELECT query_kind, Settings FROM system.query_log "
        "WHERE type = 'QueryFinish' AND has(tables, {t:String}) "
        "AND query_kind IN ('Select', 'Insert') ORDER BY event_time_microseconds",
        parameters={"t": f"{database}.{table}"},
    ).result_rows
    return [(str(kind), dict(settings)) for kind, settings in rows]


def _assert_consistent(queries: list[tuple[str, dict[str, str]]], *, quorum: str) -> None:
    reads = [settings for kind, settings in queries if kind == "Select"]
    writes = [settings for kind, settings in queries if kind == "Insert"]
    assert reads, queries
    assert writes, queries
    for settings in reads:
        assert settings.get("select_sequential_consistency") == "1", settings
    for settings in writes:
        assert settings.get("insert_quorum") == quorum, settings
        assert settings.get("insert_quorum_parallel") == "0", settings
        assert settings.get("async_insert") == "0", settings
        # A claim acknowledged after its own lease lapsed would be held by nobody who knows it.
        assert int(settings.get("insert_quorum_timeout", "600000")) < 60_000, settings


def test_lease_reads_and_writes_carry_the_consistency_settings(ch_client, empty_database, plan):
    """One node: a quorum of one, which ClickHouse treats as no quorum wait at all."""
    apply_plan(ch_client, plan)
    table = plan.by_id(LOCK_ID).name
    lock = SchemaLock(
        ch_client, database=empty_database, table=table, holder="pod-s", lease_seconds=60
    )

    assert lock.acquire(wait_seconds=_RIDE_OUT) is True
    lock.release()

    queries = _lease_queries(ch_client, empty_database, table)
    assert [kind for kind, _ in queries].count("Insert") >= 2, queries
    _assert_consistent(queries, quorum="1")


@pytest.fixture
def replicated_lease(two_replicas):
    """The lease table on both replicas of a Replicated database, and a lock factory per replica."""
    database = f"dfe_lease_{uuid.uuid4().hex[:8]}"
    table = render_one(LOCK_ID, data_database=database).name
    r1 = clickhouse_connect.get_client(**two_replicas.first)
    r2 = clickhouse_connect.get_client(**two_replicas.second)
    r1.command(create_database(database, Platform.SELF_HOSTED))
    for statement in render_one(LOCK_ID, data_database=database).statements:
        r1.command(statement)

    def registered() -> int:
        r2.command(f"SYSTEM SYNC DATABASE REPLICA `{database}`")
        rows = r1.query(
            "SELECT total_replicas FROM system.replicas WHERE database = {db:String} "
            "AND table = {tbl:String}",
            parameters={"db": database, "tbl": table},
        ).result_rows
        return int(rows[0][0]) if rows else 0

    deadline = time.monotonic() + 60
    while registered() < 2:
        if time.monotonic() >= deadline:
            pytest.fail("the lease table did not reach replica 2")
        time.sleep(0.5)

    def lock(params: dict, holder: str, lease_seconds: int = 60) -> SchemaLock:
        return SchemaLock(
            clickhouse_connect.get_client(**params),
            database=database,
            table=table,
            holder=holder,
            lease_seconds=lease_seconds,
        )

    def fetches(verb: str, *servers: dict) -> None:
        # A fresh client each time: a quorum wait outlasts the server's HTTP keep-alive.
        for params in servers:
            client = clickhouse_connect.get_client(**params)
            client.command(f"SYSTEM {verb} FETCHES `{database}`.`{table}`")

    return SimpleNamespace(database=database, table=table, lock=lock, fetches=fetches)


def _race(params: list[tuple[dict, str]], build, *, wait: float) -> dict[str, bool | Exception]:
    """Start one acquire per (params, holder) at the same instant, and collect each outcome."""
    start = threading.Barrier(len(params), timeout=60)
    outcomes: dict[str, bool | Exception] = {}

    def race(server: dict, holder: str) -> None:
        try:
            lock = build(server, holder)
            start.wait()
            outcomes[holder] = lock.acquire(wait_seconds=wait)
        except Exception as exc:  # a racer that raises must fail the test, not drop out of it
            start.abort()
            outcomes[holder] = exc

    threads = [threading.Thread(target=race, args=pair) for pair in params]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return outcomes


def test_claimers_racing_across_two_replicas_leave_one_holder(two_replicas, replicated_lease):
    """Each claim reaches both replicas before it is acknowledged, so every racer reads every claim."""
    lease = replicated_lease
    servers = [two_replicas.first, two_replicas.second] * 2
    outcomes = _race(
        [(server, f"pod-{n}") for n, server in enumerate(servers)], lease.lock, wait=_RIDE_OUT
    )

    errors = [outcome for outcome in outcomes.values() if isinstance(outcome, Exception)]
    assert not errors, errors
    assert len(outcomes) == len(servers)
    assert list(outcomes.values()).count(True) == 1, outcomes
    queries: list[tuple[str, dict[str, str]]] = []
    for params in (two_replicas.first, two_replicas.second):
        client = clickhouse_connect.get_client(**params)
        queries += _lease_queries(client, lease.database, lease.table)
    _assert_consistent(queries, quorum="2")


def test_claimers_on_replicas_that_cannot_fetch_from_each_other_never_both_hold(
    two_replicas, replicated_lease
):
    """Replication stalled both ways: each replica sees only its own claim, so neither may stand."""
    lease = replicated_lease
    servers = (two_replicas.first, two_replicas.second)
    lease.fetches("STOP", *servers)
    try:
        outcomes = _race(
            [(two_replicas.first, "pod-r1"), (two_replicas.second, "pod-r2")],
            lease.lock,
            wait=0.0,
        )
    finally:
        lease.fetches("START", *servers)

    errors = [outcome for outcome in outcomes.values() if isinstance(outcome, Exception)]
    assert not errors, errors
    assert outcomes == {"pod-r1": False, "pod-r2": False}, outcomes


def test_a_claim_short_of_its_quorum_is_not_taken_and_is_once_the_replica_returns(
    two_replicas, replicated_lease
):
    """A replica down is a missed attempt that retries within the wait, never a raise."""
    lease = replicated_lease
    first = clickhouse_connect.get_client(**two_replicas.first)
    two_replicas.stop_second()

    def active() -> int:
        rows = first.query(
            "SELECT active_replicas FROM system.replicas WHERE database = {db:String} "
            "AND table = {tbl:String}",
            parameters={"db": lease.database, "tbl": lease.table},
        ).result_rows
        return int(rows[0][0])

    # A claim written while replica 2 still counts as active would wait out its quorum instead.
    deadline = time.monotonic() + 60
    while active() > 1:
        if time.monotonic() >= deadline:
            pytest.fail("replica 2 still counts as active after it stopped")
        time.sleep(0.5)

    lock = lease.lock(two_replicas.first, "pod-q")
    started = time.monotonic()
    assert lock.acquire(wait_seconds=3.0) is False
    elapsed = time.monotonic() - started
    assert lock.acquired is False
    assert elapsed >= 3.0, f"gave up after {elapsed:.1f}s, before its wait ran out"
    assert lock.read() is None, "a claim short of its quorum became visible"

    two_replicas.start_second()

    assert lock.acquire(wait_seconds=60.0) is True
    lock.release()


def test_a_claim_that_lands_slower_than_the_settle_does_not_stand(two_replicas, replicated_lease):
    """A rival's re-read may have missed a slow claim, so it is not kept, and it holds nobody back."""
    lease = replicated_lease
    held_back = lock_module._SETTLE_SECONDS + 1.0
    lease.fetches("STOP", two_replicas.second)
    # Replica 2 resumes fetching after the claim is written, so its quorum lands that late.
    resume = threading.Timer(held_back, lease.fetches, args=("START", two_replicas.second))
    slow = lease.lock(two_replicas.first, "pod-slow")
    resume.start()
    try:
        started = time.monotonic()
        taken = slow.acquire(wait_seconds=0.0)
        elapsed = time.monotonic() - started
    finally:
        resume.join()
        lease.fetches("START", two_replicas.second)

    assert elapsed >= held_back, f"the claim landed after {elapsed:.2f}s, not held back"
    assert taken is False, "a claim that landed after the settle stood"
    latest = slow.read()
    assert latest is not None
    # Releasing it could free a rival that stood while it waited, so the row stays unreleased.
    assert (latest.holder, latest.released) == ("pod-slow", False), latest
    # Its marker is ended, so the next applier takes the lease instead of waiting it out.
    following = lease.lock(two_replicas.second, "pod-next")
    assert following.acquire(wait_seconds=0.0) is True
    following.release()


def test_a_slow_claim_is_recognised_as_its_own_and_stands_within_a_settle(
    two_replicas, replicated_lease
):
    """No rival holds a live claim, so the lock's next attempt claims afresh instead of waiting out its own."""
    lease = replicated_lease
    lease.fetches("STOP", two_replicas.second)
    resume = threading.Timer(
        lock_module._SETTLE_SECONDS + 1.0, lease.fetches, args=("START", two_replicas.second)
    )
    slow = lease.lock(two_replicas.first, "pod-slow")
    resume.start()
    try:
        assert slow.acquire(wait_seconds=0.0) is False
    finally:
        resume.join()
        lease.fetches("START", two_replicas.second)

    started = time.monotonic()
    taken = slow.acquire(wait_seconds=lock_module.lease_seconds(0.0))
    elapsed = time.monotonic() - started

    assert taken is True, "the lock waited on its own claim"
    assert elapsed < 2 * lock_module._SETTLE_SECONDS + 1.0, f"stood after {elapsed:.1f}s"
    slow.release()


@pytest.mark.parametrize("evidence", ["claim row", "live marker"])
def test_a_rival_live_claim_beside_a_slow_one_is_still_waited_on(
    two_replicas, replicated_lease, evidence
):
    """A rival that may have stood keeps the lease, whether its row is unmerged or only its marker is left."""
    lease = replicated_lease
    slow = lease.lock(two_replicas.first, "pod-slow")
    rival = lease.lock(two_replicas.first, "pod-rival")
    now = datetime.now(UTC)
    earlier = lock_module._millis(now - timedelta(seconds=3))
    expires = now + timedelta(seconds=300)
    if evidence == "claim row":
        # The rival's claim sits beside the slow one, as before a merge collapses them.
        rival._write(earlier, expires=expires, released=0)
    else:
        # A merge has kept only the later row, and the rival's marker is what is left of it.
        rival._write(earlier, expires=expires, released=0, lock_name=rival._marker(earlier))
    claimed = slow._claim(now)
    slow._end_marker(claimed, expires)
    state = slow.read()
    assert state is not None
    assert state.holder == "pod-slow"

    assert slow._only_ended_claims(now) is False


def test_a_slow_claim_never_frees_a_rival_that_stood(two_replicas, replicated_lease):
    """Y stands while X's later-versioned claim waits on its quorum, so X must not release it.

    X's lease is shorter than Y's, as a caller with a shorter wait writes, so X's row
    expires while Y still holds. Neither that nor a merge that folds Y's row away
    may let a third applier in.
    """
    lease = replicated_lease
    y = lease.lock(two_replicas.first, "pod-y", lease_seconds=300)
    x = lease.lock(two_replicas.second, "pod-x", lease_seconds=3)
    observer = lease.lock(two_replicas.first, "pod-observer")
    # Replica 1 stops fetching, so X's claim on replica 2 waits for its quorum.
    lease.fetches("STOP", two_replicas.first)
    try:
        # Both find the lease free, Y first, so X's claim carries the later version.
        started_y = time.monotonic()
        assert y.read() is None
        now_y = datetime.now(UTC)
        time.sleep(0.01)
        started_x = time.monotonic()
        assert x.read() is None
        now_x = datetime.now(UTC)
        outcomes: dict[str, bool | Exception] = {}

        # Private steps: no public path makes X write between Y's ack and Y's re-read.
        def claim(name: str, lock: SchemaLock, now: datetime, started: float) -> None:
            try:
                outcomes[name] = lock._try_claim(now, started=started)
            except Exception as exc:  # X's refusal is the outcome under test
                outcomes[name] = exc

        y_claims = threading.Thread(target=claim, args=("y", y, now_y, started_y))
        y_claims.start()
        deadline = time.monotonic() + 30
        while observer.read() is None:
            if time.monotonic() >= deadline:
                pytest.fail("Y's claim never landed")
            time.sleep(0.05)
        x_claims = threading.Thread(target=claim, args=("x", x, now_x, started_x))
        x_claims.start()
        y_claims.join()
    finally:
        lease.fetches("START", two_replicas.first)
    x_claims.join()

    assert outcomes["y"] is True, outcomes
    assert isinstance(outcomes["x"], lock_module._SlowClaim), outcomes
    taken = lease.lock(two_replicas.first, "pod-z").acquire(wait_seconds=0.0)
    holders = [name for name, held in (("pod-y", outcomes["y"]), ("pod-z", taken)) if held is True]
    assert holders == ["pod-y"], f"two holders: {holders}"

    # X's later-versioned row is the latest and lapses first, while Y's lease runs on.
    deadline = time.monotonic() + 30
    while (latest := observer.read()) is None or latest.expires_at > datetime.now(UTC):
        if time.monotonic() >= deadline:
            pytest.fail(f"X's claim never lapsed: {latest}")
        time.sleep(0.5)
    assert latest.holder == "pod-x", latest
    assert lease.lock(two_replicas.first, "pod-after-lapse").acquire(wait_seconds=0.0) is False, (
        "a lapsed missed claim let a third applier in beside Y"
    )

    first = clickhouse_connect.get_client(**two_replicas.first)
    first.command(f"OPTIMIZE TABLE `{lease.database}`.`{lease.table}` FINAL")
    assert lease.lock(two_replicas.first, "pod-after-merge").acquire(wait_seconds=0.0) is False, (
        "a merge that folded Y's row away let a third applier in beside Y"
    )


def test_an_unreadable_lease_table_fails_the_pass_rather_than_applying_unleased(
    ch_client, ch_params, empty_database, plan
):
    """A pass that cannot tell whether the lease table exists must not apply as if it did not."""
    apply_plan(ch_client, plan)
    table = plan.by_id(LOCK_ID).name
    holder = SchemaLock(
        ch_client, database=empty_database, table=table, holder="pod-a", lease_seconds=60
    )
    assert holder.acquire(wait_seconds=_RIDE_OUT) is True
    refused = clickhouse_connect.get_client(**ch_params)
    # Every query this client sends is now refused for memory, a refusal a later pass can clear.
    refused.set_client_setting("max_untracked_memory", 0)
    refused.set_client_setting("max_memory_usage", 1)
    lock = SchemaLock(
        refused, database=empty_database, table=table, holder="pod-b", lease_seconds=60
    )
    state = phase.SchemaBootstrapState(state=phase.STATE_RUNNING)

    with pytest.raises(SchemaLockError) as raised:
        phase._apply_under_lock(ch_client, plan, lock, wait=0.0, allow_drift=False, state=state)

    assert str(raised.value).startswith("could not read the schema lock table"), raised.value
    assert phase.is_transient_failure(raised.value) is True, raised.value
    held = holder.read()
    assert held is not None
    assert (held.holder, held.released) == ("pod-a", False)


def test_a_release_that_fails_after_the_apply_keeps_the_apply(
    ch_client, ch_params, empty_database, plan
):
    """The lease lapses at its expiry, so a refused release must not fail a converged apply."""
    apply_plan(ch_client, plan)
    table = plan.by_id(LOCK_ID).name
    suffix = uuid.uuid4().hex[:8]
    user, quota, password = f"lease_{suffix}", f"lease_one_insert_{suffix}", secrets.token_hex(16)
    ch_client.command(f"CREATE USER `{user}` IDENTIFIED WITH sha256_password BY '{password}'")
    try:
        ch_client.command(f"GRANT SELECT, INSERT ON `{empty_database}`.`{table}` TO `{user}`")
        ch_client.command(f"GRANT SELECT ON system.replicas TO `{user}`")
        # The claim is this user's one insert, so its release is refused. A quota
        # window resets at fixed moments, so a year-long one keeps the claim and the
        # release in the same window.
        ch_client.command(
            f"CREATE QUOTA `{quota}` FOR INTERVAL 1 year MAX query_inserts = 1 TO `{user}`"
        )
        lock = SchemaLock(
            clickhouse_connect.get_client(
                host=ch_params["host"],
                port=ch_params["port"],
                username=user,
                password=password,
                secure=ch_params["secure"],
            ),
            database=empty_database,
            table=table,
            holder="pod-r",
            lease_seconds=60,
        )
        state = phase.SchemaBootstrapState(state=phase.STATE_RUNNING)

        report = phase._apply_under_lock(
            ch_client, plan, lock, wait=0.0, allow_drift=False, state=state
        )

        assert report is not None
        assert not report.refused, [outcome.describe() for outcome in report.refused]
        assert state.state == phase.STATE_RUNNING, state.error
        assert state.lease_release_failed is True
        held = lock.read()
        assert held is not None
        assert (held.holder, held.released) == ("pod-r", False), "the refused release landed"
    finally:
        ch_client.command(f"DROP QUOTA IF EXISTS `{quota}`")
        ch_client.command(f"DROP USER IF EXISTS `{user}`")
