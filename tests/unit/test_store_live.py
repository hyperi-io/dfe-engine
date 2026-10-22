#  Project:      dfe-engine
#  File:         tests/unit/test_store_live.py
#  Purpose:      A value built from a collection, built again when its write counter moves
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""``Live`` over a plain counter and builder: what it builds, and when it builds again."""

import asyncio
import threading
import time
from collections.abc import Callable

from dfe_engine.store.live import Live

# A backstop for each wait on the background check: only a failing run waits it out.
_WAIT_SECONDS = 10.0


def _until(check: Callable[[], bool]) -> None:
    deadline = time.monotonic() + _WAIT_SECONDS
    while not check():
        assert time.monotonic() < deadline, "the background check never finished"
        time.sleep(0.01)


def _read_on_a_loop(live: Live[int]) -> int:
    async def read() -> int:
        return live.get()

    return asyncio.run(read())


class _Collection:
    """A write counter and a builder that numbers each build."""

    def __init__(self) -> None:
        self.revision = 0
        self.builds = 0

    def build(self) -> int:
        self.builds += 1
        return self.builds


def test_it_builds_once_up_front():
    collection = _Collection()

    live = Live(collection.build, lambda: collection.revision, refresh_seconds=0)

    assert (live.value, collection.builds) == (1, 1)


def test_an_unmoved_counter_keeps_the_value():
    collection = _Collection()
    live = Live(collection.build, lambda: collection.revision, refresh_seconds=0)

    assert [live.get() for _ in range(3)] == [1, 1, 1]


def test_a_moved_counter_builds_again():
    collection = _Collection()
    live = Live(collection.build, lambda: collection.revision, refresh_seconds=0)

    collection.revision += 1

    assert (live.get(), live.get()) == (2, 2)


def test_within_the_floor_the_counter_is_not_read():
    collection = _Collection()
    reads: list[int] = []

    def revision() -> int:
        reads.append(collection.revision)
        return collection.revision

    live = Live(collection.build, revision, refresh_seconds=3600)
    collection.revision += 1

    assert (live.get(), len(reads)) == (1, 1)


def test_refresh_builds_now_whatever_the_floor():
    collection = _Collection()
    live = Live(collection.build, lambda: collection.revision, refresh_seconds=3600)

    assert (live.refresh(), live.value) == (2, 2)


def test_a_build_finishing_after_a_newer_one_is_built_again_at_the_next_check():
    """The writer's own refresh can land while another thread's slower build is still running."""
    collection = _Collection()
    live: Live[int] | None = None

    def build() -> int:
        value = collection.build()
        if value == 2:
            # Another replica writes, and this replica's writer refreshes, mid-build.
            collection.revision += 1
            assert live is not None
            live.refresh()
        return value

    live = Live(build, lambda: collection.revision, refresh_seconds=0)
    assert live.refresh() == 2

    assert live.get() == 4


def test_on_an_event_loop_a_read_returns_at_once_and_the_check_runs_behind_it():
    collection = _Collection()
    released = threading.Event()

    def revision() -> int:
        # A store that holds every read once the counter has moved, until the test releases it.
        if collection.revision and not released.wait(timeout=_WAIT_SECONDS):
            raise TimeoutError("never released")
        return collection.revision

    live = Live(collection.build, revision, refresh_seconds=0)
    collection.revision += 1

    started = time.monotonic()
    assert _read_on_a_loop(live) == 1
    assert time.monotonic() - started < 1.0
    released.set()

    _until(lambda: live.value == 2)


def test_a_failing_background_check_keeps_the_value_and_logs_once_until_it_recovers(
    audit_events,
):
    collection = _Collection()
    down = threading.Event()

    def revision() -> int:
        if down.is_set():
            raise ConnectionError("store unreachable")
        return collection.revision

    live = Live(collection.build, revision, refresh_seconds=0)
    down.set()
    for _ in range(3):
        assert _read_on_a_loop(live) == 1
        _until(lambda: not live._checking)
    down.clear()
    collection.revision += 1
    _read_on_a_loop(live)
    # The value lands before the check logs its recovery, so wait for the whole check.
    _until(lambda: live.value == 2 and not live._checking)

    events = [e["event"] for e in audit_events if "replicated value" in e["event"]]
    assert events == [
        "A replicated value could not be checked against its store; serving it as built",
        "A replicated value is being checked against its store again",
    ]
