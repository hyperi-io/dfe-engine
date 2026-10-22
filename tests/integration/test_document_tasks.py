#  Project:      dfe-engine
#  File:         tests/integration/test_document_tasks.py
#  Purpose:      Task status shared by two engine replicas through a real document store
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Two task managers on one document store, as two engine replicas hold them.

Runs against the harness's document store (``mongo_uri``: ``DFE_TEST_MONGO_URI``, else
DocumentDB on docker), each test in a throwaway database dropped on teardown. Each
replica has its own client and its own owner id, so only the store carries a task from
one to the other. The killed-owner test runs its owner as a child process and SIGKILLs
it, so nothing records how its task ended.
"""

import asyncio
import logging
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from dfe_engine.api.document_tasks import (
    HEARTBEAT_SECONDS,
    OWNER_LOST_SECONDS,
    TASKS_COLLECTION,
    DocumentTaskManager,
)
from dfe_engine.api.task_manager import (
    TASK_STOPPED_MESSAGE,
    TaskInfo,
    TaskLimitError,
    TaskManager,
    TaskStatus,
)
from dfe_engine.store.documents import DocuStore

pytestmark = pytest.mark.integration

_log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
OWNER_PROBE = "tests/support/task_owner_probe.py"
PROBE_KIND = "test:owner-killed"

LIMIT = 3
RACE_ROUNDS = 10
SUBMITS_PER_REPLICA = 4


@pytest.fixture
def database() -> str:
    return f"dfe_engine_test_{uuid.uuid4().hex[:8]}"


@pytest.fixture
def stores(mongo_uri: str, database: str) -> Iterator[tuple[DocuStore, DocuStore]]:
    """Two clients on one database, as two replicas hold them."""
    first, second = DocuStore(mongo_uri, database), DocuStore(mongo_uri, database)
    first.ping()
    try:
        yield first, second
    finally:
        first.drop_database(database)
        first.close()
        second.close()


@pytest.fixture
async def replicas(
    stores: tuple[DocuStore, DocuStore],
) -> AsyncIterator[tuple[TaskManager, TaskManager]]:
    """Replica A and replica B, each with its own client and owner id."""
    a = DocumentTaskManager(stores[0], owner="replica-a")
    b = DocumentTaskManager(stores[1], owner="replica-b")
    try:
        yield a, b
    finally:
        await a.aclose()
        await b.aclose()


async def _until(
    read: Callable[[], TaskInfo | None],
    done: Callable[[TaskInfo], bool],
    *,
    timeout: float = 10.0,
    what: str,
) -> TaskInfo:
    """Poll ``read`` until it returns a task ``done`` accepts, failing after ``timeout``."""
    deadline = time.monotonic() + timeout
    latest = None
    while time.monotonic() < deadline:
        latest = read()
        if latest is not None and done(latest):
            return latest
        await asyncio.sleep(0.1)
    raise AssertionError(f"{what} not seen within {timeout}s, last read {latest!r}")


async def _wait_forever(*, task: Any) -> None:
    await asyncio.Event().wait()


def _running(info: TaskInfo) -> bool:
    return info.status is TaskStatus.RUNNING


def _status(manager: TaskManager, task_id: str) -> TaskStatus | None:
    info = manager.get(task_id)
    return None if info is None else info.status


class TestAcrossReplicas:
    """A task submitted to A is read, listed and cancelled through B."""

    async def test_a_cancel_through_b_reaches_the_coroutine_on_a_within_two_seconds(self, replicas):
        a, b = replicas
        loop = asyncio.get_running_loop()
        cancelled_at: list[float] = []

        async def until_cancelled(*, task: Any) -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled_at.append(loop.time())
                raise

        info = a.submit("test:cancel", until_cancelled)
        seen = await _until(lambda: b.get(info.id), _running, what="A's task running on B")
        assert seen.kind == "test:cancel"
        assert [t.id for t in b.list(kind="test:cancel")] == [info.id]

        asked_at = loop.time()
        assert b.cancel(info.id) is True
        await _until(lambda: a.get(info.id), lambda i: bool(cancelled_at), what="the cancel on A")

        _log.info("cancel through B reached A after %.3f s", cancelled_at[0] - asked_at)
        assert cancelled_at[0] - asked_at <= 2.0, cancelled_at[0] - asked_at
        ended = await _until(lambda: b.get(info.id), lambda i: i.status.is_terminal, what="end")
        assert ended.status is TaskStatus.CANCELLED

    async def test_progress_and_result_written_on_a_read_on_b(self, replicas):
        a, b = replicas
        release = asyncio.Event()

        async def report(*, task: Any) -> dict:
            task.set_progress(40, "forty")
            await release.wait()
            # Keys the store itself may refuse, so the result travels as JSON text.
            return {"_json.data_stream.dataset": "cisco_ios.log", "$weird": [1, None]}

        info = a.submit("test:report", report)
        halfway = await _until(
            lambda: b.get(info.id), lambda i: i.progress == 40, what="progress on B"
        )
        assert halfway.message == "forty"
        release.set()

        done = await _until(lambda: b.get(info.id), lambda i: i.status.is_terminal, what="end")
        assert done.status is TaskStatus.COMPLETED
        assert done.result == {"_json.data_stream.dataset": "cisco_ios.log", "$weird": [1, None]}
        assert done.progress == 100

    async def test_b_holds_a_reader_to_the_orgs_a_held_the_task_to(self, replicas):
        a, b = replicas
        info = a.submit("test:held", _wait_forever, held_to=["org-a"])
        await _until(lambda: b.get(info.id), _running, what="A's task on B")

        assert b.get(info.id, reader_orgs=["org-a", "org-b"]) is not None
        assert b.get(info.id, reader_orgs=["org-b"]) is None
        assert b.list(reader_orgs=["org-b"]) == []

    async def test_an_owner_whose_task_was_failed_elsewhere_stops_it(self, replicas, stores):
        a, b = replicas
        stopped = asyncio.Event()

        async def until_stopped(*, task: Any) -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                stopped.set()
                raise

        info = a.submit("test:stalled", until_stopped)
        await _until(lambda: b.get(info.id), _running, what="A's task on B")

        raw = stores[1].collection(TASKS_COLLECTION)
        stale = time.time() - OWNER_LOST_SECONDS - 1

        def backdated_read() -> TaskInfo | None:
            # A stalled owner, as B sees one: its last beat older than the window.
            raw.update_one({"id": info.id}, {"$set": {"heartbeat_at": stale}})
            return b.get(info.id)

        failed = await _until(
            backdated_read, lambda i: i.status is TaskStatus.FAILED, what="B failing it"
        )
        assert failed.error == TASK_STOPPED_MESSAGE

        await asyncio.wait_for(stopped.wait(), timeout=HEARTBEAT_SECONDS + 3)
        await asyncio.sleep(HEARTBEAT_SECONDS)
        assert _status(b, info.id) is TaskStatus.FAILED


class TestOwnerLost:
    """A killed owner's task reads as failed on B once its heartbeat runs out."""

    def test_b_reports_the_task_of_a_killed_owner_failed_within_35_seconds(
        self, mongo_uri, database, stores, tmp_path
    ):
        b = DocumentTaskManager(stores[1], owner="replica-b")
        raw = stores[1].collection(TASKS_COLLECTION)
        env = dict(os.environ)
        env.pop("PYTEST_ADDOPTS", None)
        env["DFE_TEST_TASK_STORE_URI"] = mongo_uri
        env["DFE_TEST_TASK_STORE_DATABASE"] = database
        log = tmp_path / "owner.log"

        with log.open("w", encoding="utf-8") as sink:
            owner = subprocess.Popen(
                [sys.executable, OWNER_PROBE],
                cwd=PROJECT_ROOT,
                env=env,
                stdout=sink,
                stderr=sink,
            )
        try:
            task = self._first_running(b, owner, log, mongo_uri)

            first_beat = raw.find_one({"id": task.id})["heartbeat_at"]
            time.sleep(HEARTBEAT_SECONDS + 2)
            assert raw.find_one({"id": task.id})["heartbeat_at"] > first_beat, "no heartbeat"
            assert _status(b, task.id) is TaskStatus.RUNNING
        finally:
            owner.send_signal(signal.SIGKILL)
            owner.wait(timeout=30)
        killed_at = time.monotonic()

        info = None
        while time.monotonic() - killed_at < 40:
            info = b.get(task.id)
            if info is not None and info.status.is_terminal:
                break
            time.sleep(0.5)
        elapsed = time.monotonic() - killed_at
        _log.info("B read the killed owner's task as failed %.3f s after the kill", elapsed)

        assert info is not None
        assert info.status is TaskStatus.FAILED, (info, elapsed)
        assert info.error == TASK_STOPPED_MESSAGE
        assert elapsed <= 35.0, elapsed
        # Its last beat was at most one heartbeat before the kill, so not failed early.
        assert elapsed >= OWNER_LOST_SECONDS - HEARTBEAT_SECONDS - 2, elapsed
        assert raw.find_one({"id": task.id})["status"] == TaskStatus.FAILED.value

    @staticmethod
    def _first_running(
        b: TaskManager, owner: subprocess.Popen, log: Path, mongo_uri: str
    ) -> TaskInfo:
        """The probe's task once B reads it running, failing if the probe dies or stalls."""
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if owner.poll() is not None:
                break
            running = [t for t in b.list(kind=PROBE_KIND) if _running(t)]
            if running:
                return running[0]
            time.sleep(0.2)
        tail = log.read_text(encoding="utf-8", errors="replace")[-2000:].replace(mongo_uri, "***")
        raise AssertionError(f"the owner's task never ran (exit {owner.poll()}): {tail}")


class TestLimitAcrossReplicas:
    """``max_active`` counts every replica's tasks of a kind, not one replica's."""

    async def test_b_is_refused_while_a_holds_every_slot_and_admitted_once_one_ends(self, replicas):
        a, b = replicas
        held = [a.submit("test:capped", _wait_forever, max_active=LIMIT) for _ in range(LIMIT)]

        with pytest.raises(TaskLimitError) as refused:
            b.submit("test:capped", _wait_forever, max_active=LIMIT)
        assert refused.value.limit == LIMIT
        assert len(b.list(kind="test:capped")) == LIMIT

        assert b.cancel(held[0].id) is True
        await _until(lambda: b.get(held[0].id), lambda i: i.status.is_terminal, what="end")

        admitted = b.submit("test:capped", _wait_forever, max_active=LIMIT)
        assert b.get(admitted.id) is not None

    def test_concurrent_submits_through_two_replicas_admit_exactly_the_limit(self, stores):
        for round_ in range(RACE_ROUNDS):
            kind = f"test:race-{round_}"
            barrier = threading.Barrier(2)

            def replica(store: DocuStore, owner: str, kind: str = kind, barrier=barrier) -> int:
                return asyncio.run(_admitted(store, owner, kind, barrier))

            with ThreadPoolExecutor(max_workers=2) as pool:
                counts = list(pool.map(replica, list(stores), ["replica-a", "replica-b"]))
            assert sum(counts) == LIMIT, (round_, counts)

    async def test_a_slot_held_by_a_killed_owner_is_taken_back(self, replicas, stores):
        a, b = replicas
        held = [a.submit("test:reclaim", _wait_forever, max_active=LIMIT) for _ in range(LIMIT)]
        for info in held:
            await _until(lambda i=info: b.get(i.id), _running, what="A's task on B")

        # A stalled owner: every slot holder's last beat older than the window.
        raw = stores[1].collection(TASKS_COLLECTION)
        raw.update_many(
            {"id": {"$in": [i.id for i in held]}},
            {
                "$set": {
                    "owner": "replica-gone",
                    "heartbeat_at": time.time() - OWNER_LOST_SECONDS - 1,
                }
            },
        )

        admitted = b.submit("test:reclaim", _wait_forever, max_active=LIMIT)
        assert _status(b, admitted.id) in (TaskStatus.PENDING, TaskStatus.RUNNING)
        assert _status(b, held[0].id) is TaskStatus.FAILED


async def _admitted(store: DocuStore, owner: str, kind: str, barrier: threading.Barrier) -> int:
    """How many of :data:`SUBMITS_PER_REPLICA` capped submits one replica gets through.

    Both replicas submit from the same barrier, and neither stops its tasks, which frees
    their slots, until both have finished submitting.
    """
    manager = DocumentTaskManager(store, owner=owner)
    barrier.wait(timeout=30)
    admitted = 0
    for _ in range(SUBMITS_PER_REPLICA):
        try:
            manager.submit(kind, _wait_forever, max_active=LIMIT)
        except TaskLimitError:
            continue
        admitted += 1
    barrier.wait(timeout=30)
    await manager.aclose()
    return admitted


class TestPrune:
    """The store keeps ``max_completed`` ended tasks across every replica."""

    async def test_the_oldest_ended_tasks_go_beyond_the_bound(self, stores):
        keep = 3
        a = DocumentTaskManager(stores[0], owner="replica-a", max_completed=keep)
        b = DocumentTaskManager(stores[1], owner="replica-b", max_completed=keep)

        async def quick(*, task: Any) -> str:
            return "ok"

        submitted: list[TaskInfo] = []
        for manager in (a, b, a, b, a):
            info = manager.submit("test:prune", quick)
            await _until(lambda i=info: b.get(i.id), lambda i: i.status.is_terminal, what="end")
            submitted.append(info)
        await _until_count(lambda: len(b.list(kind="test:prune")), keep)

        kept = {t.id for t in b.list(kind="test:prune")}
        assert kept == {t.id for t in submitted[-keep:]}
        await a.aclose()
        await b.aclose()


async def _until_count(count: Callable[[], int], expected: int, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    seen = count()
    while seen != expected and time.monotonic() < deadline:
        await asyncio.sleep(0.1)
        seen = count()
    assert seen == expected, seen
