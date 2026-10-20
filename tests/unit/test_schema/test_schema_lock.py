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

import pytest
from clickhouse_connect.driver.exceptions import DatabaseError, OperationalError

from dfe_engine.schema.lock import SchemaLockError, SchemaLockQuorumError, _lock_error, _majority


@pytest.mark.parametrize(
    ("replicas", "quorum"),
    [(0, 1), (1, 1), (2, 2), (3, 2), (4, 3), (5, 3)],
)
def test_the_quorum_is_a_majority_and_one_replica_waits_for_none(replicas, quorum):
    """A quorum of one is the server's no-quorum value, which ``auto`` never gives one replica."""
    assert _majority(replicas) == quorum


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
