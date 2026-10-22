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
it, so nothing records how its task ended. The stalled-store test reaches the store
through a relay it holds (:mod:`tests.support.pausable_relay`). The crash between a
result's chunks and its record holds the owner's record write, and backdates the
heartbeat and the set's header as the window passing would.
"""

import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import anyio
import pytest
from pymongo.collection import Collection

from dfe_engine.api import document_tasks
from dfe_engine.api.document_tasks import (
    HEARTBEAT_SECONDS,
    OWNER_LOST_SECONDS,
    RESULT_SETS_COLLECTION,
    RESULTS_COLLECTION,
    TASKS_COLLECTION,
    WATCH_SECONDS,
    DocumentTaskManager,
    TaskResultMissingError,
)
from dfe_engine.api.task_manager import (
    TASK_STOPPED_MESSAGE,
    TaskInfo,
    TaskLimitError,
    TaskManager,
    TaskStatus,
)
from dfe_engine.store.documents import DocuStore
from tests.support.pausable_relay import PausableRelay
from tests.support.stalled_call import Stall

pytestmark = pytest.mark.integration

_log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
OWNER_PROBE = "tests/support/task_owner_probe.py"
PROBE_KIND = "test:owner-killed"

LIMIT = 3
RACE_ROUNDS = 10
SUBMITS_PER_REPLICA = 4
# Well under the stall, so a write made during it fails rather than waiting it out.
RELAYED_SOCKET_TIMEOUT = 2.0
# Rows of 1000 characters, so the result is about 20 MB of JSON: more than one document holds.
LARGE_ROWS = 20_000
# Small chunks, so a short result spans several.
SMALL_CHUNK = 64
# 300 characters, so its JSON is 302: four full small chunks and a fifth of 46.
SPANNING = "y" * 300


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
    read: Callable[[], Awaitable[TaskInfo | None]],
    done: Callable[[TaskInfo], bool],
    *,
    timeout: float = 10.0,
    what: str,
) -> TaskInfo:
    """Poll ``read`` until it returns a task ``done`` accepts, failing after ``timeout``."""
    deadline = time.monotonic() + timeout
    latest = None
    while time.monotonic() < deadline:
        latest = await read()
        if latest is not None and done(latest):
            return latest
        await asyncio.sleep(0.1)
    raise AssertionError(f"{what} not seen within {timeout}s, last read {latest!r}")


async def _wait_forever(*, task: Any) -> None:
    await asyncio.Event().wait()


def _running(info: TaskInfo) -> bool:
    return info.status is TaskStatus.RUNNING


async def _status(manager: TaskManager, task_id: str) -> TaskStatus | None:
    info = await manager.get(task_id)
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

        info = await a.submit("test:cancel", until_cancelled)
        seen = await _until(lambda: b.get(info.id), _running, what="A's task running on B")
        assert seen.kind == "test:cancel"
        assert [t.id for t in await b.list(kind="test:cancel")] == [info.id]

        asked_at = loop.time()
        assert await b.cancel(info.id) is True
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

        info = await a.submit("test:report", report)
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
        info = await a.submit("test:held", _wait_forever, held_to=["org-a"])
        await _until(lambda: b.get(info.id), _running, what="A's task on B")

        assert await b.get(info.id, reader_orgs=["org-a", "org-b"]) is not None
        assert await b.get(info.id, reader_orgs=["org-b"]) is None
        assert await b.list(reader_orgs=["org-b"]) == []

    async def test_an_owner_whose_task_was_failed_elsewhere_stops_it(self, replicas, stores):
        a, b = replicas
        stopped = asyncio.Event()

        async def until_stopped(*, task: Any) -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                stopped.set()
                raise

        info = await a.submit("test:stalled", until_stopped)
        await _until(lambda: b.get(info.id), _running, what="A's task on B")

        raw = stores[1].collection(TASKS_COLLECTION)
        stale = time.time() - OWNER_LOST_SECONDS - 1

        async def backdated_read() -> TaskInfo | None:
            # A stalled owner, as B sees one: its last beat older than the window.
            raw.update_one({"id": info.id}, {"$set": {"heartbeat_at": stale}})
            return await b.get(info.id)

        failed = await _until(
            backdated_read, lambda i: i.status is TaskStatus.FAILED, what="B failing it"
        )
        assert failed.error == TASK_STOPPED_MESSAGE

        await asyncio.wait_for(stopped.wait(), timeout=HEARTBEAT_SECONDS + 3)
        await asyncio.sleep(HEARTBEAT_SECONDS)
        assert await _status(b, info.id) is TaskStatus.FAILED


async def _until_true(check: Callable[[], bool], *, timeout: float = 10.0, what: str) -> None:
    deadline = time.monotonic() + timeout
    while not check():
        if time.monotonic() >= deadline:
            raise AssertionError(f"{what} not seen within {timeout}s")
        await asyncio.sleep(0.05)


async def _beat_after(raw: Collection, task_id: str, after: float, *, timeout: float) -> bool:
    """Whether the stored heartbeat of *task_id* moves past *after* within *timeout* seconds.

    Reads on this thread, since the test may hold every shared worker thread.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        stored = raw.find_one({"id": task_id})
        if stored is not None and stored["heartbeat_at"] > after:
            return True
        await asyncio.sleep(0.2)
    return False


class TestHeartbeat:
    """The owner keeps beating while its reads stall, and beats again once its store answers."""

    async def test_reads_stalled_on_every_shared_worker_thread_leave_the_heartbeat_beating(
        self, replicas, stores, monkeypatch
    ):
        a, b = replicas
        info = await a.submit("test:beating", _wait_forever)
        await _until(lambda: b.get(info.id), _running, what="A's task on B")
        raw = stores[1].collection(TASKS_COLLECTION)
        shared = anyio.to_thread.current_default_thread_limiter()
        stall = Stall()
        monkeypatch.setattr(a._records, "get", stall.call(a._records.get))

        reads = [asyncio.create_task(a.get(info.id)) for _ in range(int(shared.total_tokens) + 1)]
        try:
            await _until_true(
                lambda: shared.available_tokens == 0, what="every shared worker thread stalled"
            )
            beating = await _beat_after(
                raw, info.id, time.time(), timeout=HEARTBEAT_SECONDS + WATCH_SECONDS + 2
            )
        finally:
            stall.release.set()
            read = await asyncio.gather(*reads)

        assert beating, "no heartbeat while stalled reads held every shared worker thread"
        assert {i.status if i is not None else None for i in read} == {TaskStatus.RUNNING}

    async def test_a_store_that_stalls_then_answers_gets_the_heartbeat_again(
        self, mongo_uri, database, stores
    ):
        parts = urlsplit(mongo_uri)
        assert parts.hostname is not None, "a single-host store URI"
        assert parts.port is not None, "a store URI that names its port"
        relay = PausableRelay((parts.hostname, parts.port))
        relayed = DocuStore(
            relay.relayed_uri(mongo_uri), database, socket_timeout=RELAYED_SOCKET_TIMEOUT
        )
        a = DocumentTaskManager(relayed, owner="replica-a")
        b = DocumentTaskManager(stores[1], owner="replica-b")
        raw = stores[1].collection(TASKS_COLLECTION)
        try:
            info = await a.submit("test:recovers", _wait_forever)
            await _until(lambda: b.get(info.id), _running, what="A's task on B")

            relay.hold()
            await asyncio.sleep(HEARTBEAT_SECONDS + 2)
            relay.release()
            answered_at = time.time()

            beating = await _beat_after(raw, info.id, answered_at, timeout=HEARTBEAT_SECONDS + 5)
            assert beating, "no heartbeat once the store answered again"
            assert await _status(b, info.id) is TaskStatus.RUNNING
        finally:
            relay.release()
            await a.aclose()
            relayed.close()
            relay.close()


class TestOwnerLost:
    """A killed owner's task reads as failed on B once its heartbeat runs out."""

    async def test_b_reports_the_task_of_a_killed_owner_failed_within_35_seconds(
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
            task = await self._first_running(b, owner, log, mongo_uri)

            first_beat = raw.find_one({"id": task.id})["heartbeat_at"]
            await asyncio.sleep(HEARTBEAT_SECONDS + 2)
            assert raw.find_one({"id": task.id})["heartbeat_at"] > first_beat, "no heartbeat"
            assert await _status(b, task.id) is TaskStatus.RUNNING
        finally:
            owner.send_signal(signal.SIGKILL)
            owner.wait(timeout=30)
        killed_at = time.monotonic()

        info = None
        while time.monotonic() - killed_at < 40:
            info = await b.get(task.id)
            if info is not None and info.status.is_terminal:
                break
            await asyncio.sleep(0.5)
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
    async def _first_running(
        b: TaskManager, owner: subprocess.Popen, log: Path, mongo_uri: str
    ) -> TaskInfo:
        """The probe's task once B reads it running, failing if the probe dies or stalls."""
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if owner.poll() is not None:
                break
            running = [t for t in await b.list(kind=PROBE_KIND) if _running(t)]
            if running:
                return running[0]
            await asyncio.sleep(0.2)
        tail = log.read_text(encoding="utf-8", errors="replace")[-2000:].replace(mongo_uri, "***")
        raise AssertionError(f"the owner's task never ran (exit {owner.poll()}): {tail}")


class TestLimitAcrossReplicas:
    """``max_active`` counts every replica's tasks of a kind, not one replica's."""

    async def test_b_is_refused_while_a_holds_every_slot_and_admitted_once_one_ends(self, replicas):
        a, b = replicas
        held = [
            await a.submit("test:capped", _wait_forever, max_active=LIMIT) for _ in range(LIMIT)
        ]

        with pytest.raises(TaskLimitError) as refused:
            await b.submit("test:capped", _wait_forever, max_active=LIMIT)
        assert refused.value.limit == LIMIT
        assert len(await b.list(kind="test:capped")) == LIMIT

        assert await b.cancel(held[0].id) is True
        await _until(lambda: b.get(held[0].id), lambda i: i.status.is_terminal, what="end")

        admitted = await b.submit("test:capped", _wait_forever, max_active=LIMIT)
        assert await b.get(admitted.id) is not None

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
        held = [
            await a.submit("test:reclaim", _wait_forever, max_active=LIMIT) for _ in range(LIMIT)
        ]
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

        admitted = await b.submit("test:reclaim", _wait_forever, max_active=LIMIT)
        assert await _status(b, admitted.id) in (TaskStatus.PENDING, TaskStatus.RUNNING)
        assert await _status(b, held[0].id) is TaskStatus.FAILED


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
            await manager.submit(kind, _wait_forever, max_active=LIMIT)
        except TaskLimitError:
            continue
        admitted += 1
    barrier.wait(timeout=30)
    await manager.aclose()
    return admitted


class TestLargeResult:
    """A result more than one document holds still reads back whole on another replica."""

    async def test_a_20_mb_result_written_on_a_reads_back_whole_on_b(self, replicas):
        a, b = replicas
        rows = [f"{n:08d}{'x' * 992}" for n in range(LARGE_ROWS)]

        async def sample(*, task: Any) -> dict:
            return {"count": LARGE_ROWS, "rows": rows}

        info = await a.submit("test:large", sample)
        ended = await _until(
            lambda: b.get(info.id), lambda i: i.status.is_terminal, timeout=60.0, what="end"
        )
        assert ended.status is TaskStatus.COMPLETED, ended.error
        assert ended.result == {"count": LARGE_ROWS, "rows": rows}


def _returning(value: Any) -> Callable[..., Awaitable[Any]]:
    async def run(*, task: Any) -> Any:
        return value

    return run


async def _until_written(manager: TaskManager, task_id: str) -> None:
    """Wait until ``manager`` has written how its task ended, its prune and sweep included."""
    await _until_true(lambda: task_id not in manager._tasks, what=f"the end of {task_id} written")


def _chunks_of(chunks: Collection, set_id: str) -> list[str]:
    """The ids of every stored chunk of ``set_id``."""
    return sorted(doc["_id"] for doc in chunks.find({"_id": {"$regex": f"^{set_id}/"}}))


class TestResultChunks:
    """A result goes into ordered chunks the record counts, and a short read is an error."""

    async def test_a_result_is_written_as_ordered_chunks_its_record_counts(
        self, replicas, stores, monkeypatch
    ):
        monkeypatch.setattr(document_tasks, "RESULT_CHUNK_BYTES", SMALL_CHUNK)
        a, b = replicas
        info = await a.submit("test:chunks", _returning(SPANNING))
        await _until_written(a, info.id)

        record = stores[1].collection(TASKS_COLLECTION).find_one({"id": info.id})
        set_id = record["result_set"]
        assert record["result_chunks"] == 5
        found = stores[1].collection(RESULTS_COLLECTION).find({"_id": {"$regex": f"^{set_id}/"}})
        data = {doc["_id"]: doc["data"] for doc in found}
        assert sorted(data) == [f"{set_id}/{n}" for n in range(5)]
        assert [len(data[f"{set_id}/{n}"]) for n in range(5)] == [64, 64, 64, 64, 46]
        assert "".join(data[f"{set_id}/{n}"] for n in range(5)) == json.dumps(SPANNING)
        header = stores[1].collection(RESULT_SETS_COLLECTION).find_one({"_id": set_id})
        assert (header["task_id"], header["chunks"]) == (info.id, 5)

        read = await b.get(info.id)
        assert read is not None
        assert read.result == SPANNING

    async def test_a_missing_chunk_is_an_error_never_a_short_result(
        self, replicas, stores, monkeypatch
    ):
        monkeypatch.setattr(document_tasks, "RESULT_CHUNK_BYTES", SMALL_CHUNK)
        a, b = replicas
        info = await a.submit("test:missing", _returning(SPANNING))
        await _until_written(a, info.id)
        record = stores[1].collection(TASKS_COLLECTION).find_one({"id": info.id})
        chunks = stores[1].collection(RESULTS_COLLECTION)
        assert chunks.delete_one({"_id": f"{record['result_set']}/2"}).deleted_count == 1

        with pytest.raises(TaskResultMissingError):
            await b.get(info.id)
        with pytest.raises(TaskResultMissingError):
            await b.list(kind="test:missing")

    async def test_a_task_pruned_while_its_result_is_read_reads_as_gone(
        self, replicas, stores, monkeypatch
    ):
        a, b = replicas
        info = await a.submit("test:pruned", _returning("gone soon"))
        await _until_written(a, info.id)
        tasks = stores[1].collection(TASKS_COLLECTION)
        set_id = tasks.find_one({"id": info.id})["result_set"]
        stall = Stall()
        monkeypatch.setattr(b._results, "read", stall.call(b._results.read))

        reading = asyncio.create_task(b.get(info.id))
        try:
            assert await asyncio.to_thread(stall.reached.wait, 10), "the read never reached"
            # As the prune deletes them: the record, then its chunks, then the header.
            tasks.delete_one({"id": info.id})
            stores[1].collection(RESULTS_COLLECTION).delete_one({"_id": f"{set_id}/0"})
            stores[1].collection(RESULT_SETS_COLLECTION).delete_one({"_id": set_id})
        finally:
            stall.release.set()

        assert await reading is None

    async def test_pruning_a_record_drops_its_chunks_and_header(self, stores):
        a = DocumentTaskManager(stores[0], owner="replica-a", max_completed=2)
        try:
            for n in range(4):
                info = await a.submit("test:prune-results", _returning(f"result {n}"))
                await _until_written(a, info.id)
        finally:
            await a.aclose()

        ended = stores[1].collection(TASKS_COLLECTION).find({"kind": "test:prune-results"})
        kept = {doc["result_set"] for doc in ended}
        headers = {doc["_id"] for doc in stores[1].collection(RESULT_SETS_COLLECTION).find()}
        chunk_sets = {
            doc["_id"].split("/")[0] for doc in stores[1].collection(RESULTS_COLLECTION).find()
        }
        assert len(kept) == 2
        assert headers == kept
        assert chunk_sets == kept


class TestCrashBetweenChunksAndRecord:
    """Chunks written by an owner that stops before its record are never named, and get swept."""

    async def test_the_chunks_name_no_task_and_go_once_the_owner_is_lost(
        self, replicas, stores, monkeypatch
    ):
        monkeypatch.setattr(document_tasks, "RESULT_CHUNK_BYTES", SMALL_CHUNK)
        a, b = replicas
        tasks = stores[1].collection(TASKS_COLLECTION)
        chunks = stores[1].collection(RESULTS_COLLECTION)
        headers = stores[1].collection(RESULT_SETS_COLLECTION)
        stall = Stall()
        record_write = a._write_record

        def stopped_before_naming(task_id: str, fields: dict[str, Any]) -> Any:
            if fields.get("result_set"):
                stall.wait()
            return record_write(task_id, fields)

        monkeypatch.setattr(a, "_write_record", stopped_before_naming)
        info = await a.submit("test:crashed", _returning(SPANNING))
        try:
            assert await asyncio.to_thread(stall.reached.wait, 10), "the owner never wrote"
            set_id = headers.find_one({"task_id": info.id})["_id"]
            assert len(_chunks_of(chunks, set_id)) == 5
            assert tasks.find_one({"id": info.id})["result_set"] is None
            seen = await b.get(info.id)
            assert seen is not None
            assert (seen.status, seen.result) == (TaskStatus.RUNNING, None)

            # Older than the window, but its task's heartbeat is fresh: the owner may yet name it.
            old = time.time() - OWNER_LOST_SECONDS - 1
            headers.update_one({"_id": set_id}, {"$set": {"written_at": old}})
            await _end_one(b, "test:sweeper")
            assert len(_chunks_of(chunks, set_id)) == 5

            # The owner's heartbeat runs out too: the sweep records it failed, then drops the set.
            tasks.update_one({"id": info.id}, {"$set": {"heartbeat_at": old}})
            await _end_one(b, "test:sweeper")
            assert _chunks_of(chunks, set_id) == []
            assert headers.find_one({"_id": set_id}) is None
        finally:
            stall.release.set()

        await _until_written(a, info.id)
        failed = await b.get(info.id)
        assert failed is not None
        assert (failed.status, failed.error, failed.result) == (
            TaskStatus.FAILED,
            TASK_STOPPED_MESSAGE,
            None,
        )
        assert tasks.find_one({"id": info.id})["result_set"] is None


async def _end_one(manager: TaskManager, kind: str) -> None:
    """Run one task to its end on ``manager``, so its ending write prunes and sweeps."""
    info = await manager.submit(kind, _returning("ok"))
    await _until_written(manager, info.id)


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
            info = await manager.submit("test:prune", quick)
            await _until(lambda i=info: b.get(i.id), lambda i: i.status.is_terminal, what="end")
            submitted.append(info)
        await _until_count(lambda: b.list(kind="test:prune"), keep)

        kept = {t.id for t in await b.list(kind="test:prune")}
        assert kept == {t.id for t in submitted[-keep:]}
        await a.aclose()
        await b.aclose()


async def _until_count(
    read: Callable[[], Awaitable[list[TaskInfo]]], expected: int, timeout: float = 10.0
) -> None:
    deadline = time.monotonic() + timeout
    seen = len(await read())
    while seen != expected and time.monotonic() < deadline:
        await asyncio.sleep(0.1)
        seen = len(await read())
    assert seen == expected, seen
