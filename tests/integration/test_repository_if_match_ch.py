#  Project:      dfe-engine
#  File:         tests/integration/test_repository_if_match_ch.py
#  Purpose:      Two writers holding the same If-Match: exactly one wins, on real ClickHouse
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The repository store's conditional write, raced by two clients on a real ClickHouse.

Both writers read the stored etag before either writes, which is the window a
separate read-then-insert leaves open. The engine's own client wrapper is used,
one ClickHouseManager per writer, as two replicas would hold.
"""

import threading
from typing import Any

import pytest

from dfe_engine.repository.store import ConflictError, RepositoryStore

pytestmark = pytest.mark.integration

_KEY = ("org", "acme", "ui", "layout")


class _ReadsThenWaits(RepositoryStore):
    """A store whose first read waits for the other writer's first read."""

    def __init__(self, *args: Any, barrier: threading.Barrier, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._barrier: threading.Barrier | None = barrier

    def get(self, *args: Any, **kwargs: Any) -> dict[str, Any] | None:
        row = super().get(*args, **kwargs)
        barrier, self._barrier = self._barrier, None
        if barrier is not None:
            barrier.wait(timeout=30)
        return row


def _manager(ch_params):
    from dfe_engine.clickhouse.clickhouse_manager import ClickHouseManager

    return ClickHouseManager(
        target_config_data={
            "ch_host": ch_params["host"],
            "ch_port": ch_params["port"],
            "ch_username": ch_params["username"],
            "ch_password": ch_params["password"],
            "ch_secure": ch_params["secure"],
        }
    )


@pytest.fixture
def repository_db(ch_client, clickhouse_test_database):
    from dfe_schemas.clickhouse import PlatformProbe

    from dfe_engine.schema.plan import render_one

    platform = PlatformProbe(ch_client).platform
    for statement in render_one(
        "data.repository", data_database=clickhouse_test_database, platform=platform
    ).statements:
        ch_client.command(statement)
    return clickhouse_test_database


def test_two_writers_with_the_same_if_match_one_wins_one_gets_412(ch_params, repository_db):
    managers = [_manager(ch_params), _manager(ch_params)]
    try:
        seed = RepositoryStore(managers[0].get_clickhouse_client(), database=repository_db)
        etag = seed.put(*_KEY, b'{"v": 0}', "application/json", "seed")["etag"]

        barrier = threading.Barrier(2)
        outcomes: dict[str, Any] = {}

        def write(name: str, manager) -> None:
            store = _ReadsThenWaits(
                manager.get_clickhouse_client(), database=repository_db, barrier=barrier
            )
            try:
                store.put(*_KEY, name.encode(), "text/plain", name, if_match=etag)
            except ConflictError as exc:
                outcomes[name] = exc
            else:
                outcomes[name] = "won"

        threads = [
            threading.Thread(target=write, args=(name, manager))
            for name, manager in zip(("a", "b"), managers, strict=True)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        winners = [name for name, outcome in outcomes.items() if outcome == "won"]
        assert len(winners) == 1, outcomes
        loser = "b" if winners == ["a"] else "a"
        assert isinstance(outcomes[loser], ConflictError)
        stored = seed.get(*_KEY)
        assert stored is not None
        assert stored["value"] == winners[0].encode()
        # The loser is told the etag that beat it, so it can re-read and retry.
        assert outcomes[loser].current_etag == stored["etag"]
    finally:
        for manager in managers:
            manager.close()


def test_sequential_writes_inside_one_millisecond_still_get_distinct_etags(
    ch_params, repository_db
):
    manager = _manager(ch_params)
    try:
        store = RepositoryStore(manager.get_clickhouse_client(), database=repository_db)
        etags = [store.put(*_KEY, str(n).encode(), "text/plain", "seed")["etag"] for n in range(5)]
        assert len(set(etags)) == 5
        with pytest.raises(ConflictError):
            store.put(*_KEY, b"stale", "text/plain", "seed", if_match=etags[0])
    finally:
        manager.close()
