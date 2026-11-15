#  Project:      dfe-engine
#  File:         tests/unit/test_schema/test_schema_lock.py
#  Purpose:      The schema lease's quorum size and how it tells a missed quorum apart
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The pure halves of the schema lease's consistency: the quorum and the refusal it reads.

The settings reaching a real server, and a missed quorum leaving the lease untaken,
are proven against ClickHouse in tests/integration/test_schema/test_manifest_bootstrap.py.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from clickhouse_connect.driver.exceptions import DatabaseError, OperationalError

from dfe_engine.clickhouse.lease import majority, write_settings_for
from dfe_engine.schema.lock import (
    LOCK_NAME,
    SchemaLock,
    SchemaLockError,
    SchemaLockQuorumError,
    _lock_error,
)


@pytest.mark.parametrize(
    ("replicas", "quorum"),
    [(0, 1), (1, 1), (2, 2), (3, 2), (4, 3), (5, 3)],
)
def test_the_quorum_is_a_majority_and_one_replica_waits_for_none(replicas, quorum):
    """A quorum of one is the server's no-quorum value, which ``auto`` never gives one replica."""
    assert majority(replicas) == quorum


@pytest.mark.parametrize("engine", ["ReplicatedReplacingMergeTree", "ReplacingMergeTree"])
def test_a_replicated_or_plain_table_takes_a_counted_quorum_and_a_synchronous_write(engine):
    assert write_settings_for(engine, 3) == {
        "insert_quorum": 2,
        "insert_quorum_parallel": 0,
        "insert_quorum_timeout": 10_000,
        "async_insert": 0,
    }


def test_a_shared_table_carries_no_quorum_settings_and_still_writes_synchronously():
    """Every SharedMergeTree insert is a quorum insert, so Cloud is never asked to read them."""
    assert write_settings_for("SharedReplacingMergeTree", 3) == {"async_insert": 0}


@pytest.mark.parametrize(
    "code",
    [
        285,  # TOO_FEW_LIVE_REPLICAS
        286,  # UNSATISFIED_QUORUM_FOR_PREVIOUS_WRITE
        289,  # REPLICA_IS_NOT_IN_QUORUM
        319,  # UNKNOWN_STATUS_OF_INSERT
    ],
)
def test_a_missed_quorum_is_its_own_failure(code):
    error = _lock_error(
        "could not write the schema lock",
        DatabaseError(f"Code: {code}. DB::Exception: quorum. (SOME_NAME)", code=code),
    )

    assert isinstance(error, SchemaLockQuorumError)
    assert str(error).startswith("could not write the schema lock: ")


@pytest.mark.parametrize(
    "cause",
    [
        DatabaseError("Code: 60. DB::Exception: Unknown table. (UNKNOWN_TABLE)", code=60),
        DatabaseError("Code: 2. DB::Exception: Async inserts with quorum. (UNSUPPORTED_PARAMETER)"),
        OperationalError("Error HTTPConnectionPool: Read timed out."),
    ],
)
def test_every_other_failure_stays_a_lease_error(cause):
    error = _lock_error("could not read the schema lock", cause)

    assert type(error) is SchemaLockError


@dataclass
class _Result:
    result_rows: list[list[Any]]


@dataclass
class _RecordingClient:
    """A client that holds no leases, and records what a lock reads and inserts."""

    reads: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    inserts: list[list[list[Any]]] = field(default_factory=list)

    def query(self, sql: str, parameters: dict[str, Any] | None = None, **_: Any) -> _Result:
        if "system.tables" in sql:
            return _Result([["ReplicatedReplacingMergeTree"]])
        if "system.replicas" in sql:
            return _Result([[3]])
        self.reads.append((sql, parameters or {}))
        return _Result([])

    def insert(self, table: str, rows: list[list[Any]], **_: Any) -> None:
        self.inserts.append(rows)


_HELD_AT = datetime(2026, 1, 2, 3, 4, 5, 678000, tzinfo=UTC)


def _lock(client: _RecordingClient, lock_name: str | None) -> SchemaLock:
    if lock_name is None:
        return SchemaLock(client, database="d", table="t", holder="pod/1")
    return SchemaLock(client, database="d", table="t", holder="pod/1", lock_name=lock_name)


@pytest.mark.parametrize(
    ("lock_name", "name"),
    [(None, LOCK_NAME), ("hunt_smoothing_leader", "hunt_smoothing_leader")],
    ids=["default", "own-lease"],
)
def test_a_claim_and_its_marker_carry_the_locks_name(lock_name, name):
    client = _RecordingClient()
    lock = _lock(client, lock_name)

    lock._claim(_HELD_AT)

    claim, marker = client.inserts[0]
    assert claim[0] == name
    assert marker[0] == f"{name}/claim/pod/1/2026-01-02T03:04:05.678+00:00"
    assert marker[1:] == claim[1:]


@pytest.mark.parametrize(
    ("lock_name", "name"),
    [(None, LOCK_NAME), ("hunt_smoothing_leader", "hunt_smoothing_leader")],
    ids=["default", "own-lease"],
)
def test_a_release_ends_the_claim_and_marker_under_the_locks_name(lock_name, name):
    client = _RecordingClient()
    lock = _lock(client, lock_name)
    lock._claimed_at = _HELD_AT
    lock.acquired = True

    lock.release()

    claim, marker = client.inserts[0]
    assert (claim[0], claim[4]) == (name, 1)
    assert marker[0].startswith(f"{name}/claim/pod/1/")
    assert marker[4] == 1


@pytest.mark.parametrize(
    ("lock_name", "name"),
    [(None, LOCK_NAME), ("hunt_smoothing_leader", "hunt_smoothing_leader")],
    ids=["default", "own-lease"],
)
def test_reads_look_up_the_locks_own_name_and_marker_prefix(lock_name, name):
    client = _RecordingClient()
    lock = _lock(client, lock_name)

    assert lock.read() is None
    assert lock._only_ended_claims(datetime.now(UTC) + timedelta(seconds=1)) is True

    assert client.reads[0][1] == {"lock": name}
    assert [p["lock"] for _, p in client.reads[1:]] == [name, name]
    assert [p["prefix"] for _, p in client.reads[1:]] == [f"{name}/claim/"] * 2


def test_an_ended_marker_write_defaults_to_the_locks_name():
    client = _RecordingClient()
    lock = _lock(client, "hunt_smoothing_leader")

    lock._write(_HELD_AT, expires=_HELD_AT, released=1)

    assert client.inserts[0][0][0] == "hunt_smoothing_leader"
