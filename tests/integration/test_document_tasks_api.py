#  Project:      dfe-engine
#  File:         tests/integration/test_document_tasks_api.py
#  Purpose:      The engine app keeps task status in the document store, through its own routes
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The engine app on a document store: its task manager, its shutdown, and its task routes.

Each test starts the engine API over the harness's ClickHouse with its users, groups and
tasks in the harness's document store (``document_engine``). A second task manager on
the same store stands in for another replica, so the routes are driven against tasks
either replica runs.
"""

import asyncio
import time
from collections.abc import Callable
from typing import Any

import pytest

from dfe_engine.api.document_tasks import TASKS_COLLECTION, DocumentTaskManager
from dfe_engine.api.task_manager import TASK_STOPPED_MESSAGE, TaskStatus
from dfe_engine.replicas import instance_id
from dfe_engine.store.documents import DocuStore

pytestmark = pytest.mark.integration

STREAM_ROUTE = "/api/v1/synthetic-data/stream"
# Low rate at an address nothing listens on, so the stream stays running until stopped.
LONG_STREAM = {
    "schema": "meta/syslog",
    "duration_s": 60,
    "rate_eps": 0.5,
    "receiver_url": "http://127.0.0.1:9/ingest",
}


def _until_sync(check: Callable[[], bool], *, timeout: float = 10.0, what: str) -> None:
    deadline = time.monotonic() + timeout
    while not check():
        if time.monotonic() >= deadline:
            raise AssertionError(f"{what} not seen within {timeout}s")
        time.sleep(0.1)


class TestEngineLifespan:
    """The lifespan builds a document task manager on the store and stops it before the store."""

    def test_the_engine_keeps_its_tasks_in_the_store_and_stops_them_before_closing_it(
        self, document_engine
    ):
        reader = DocuStore(document_engine.store_uri, document_engine.store_database)
        raw = reader.collection(TASKS_COLLECTION)
        try:
            with document_engine.start() as stack:
                assert isinstance(stack.app.state.task_manager, DocumentTaskManager)
                started = stack.client.post(STREAM_ROUTE, json=LONG_STREAM, headers=stack.headers)
                assert started.status_code == 200, started.text
                task_id = started.json()["task_id"]
                _until_sync(
                    lambda: (raw.find_one({"id": task_id}) or {}).get("status") == "running",
                    what="the stream running in the store",
                )
                assert (raw.find_one({"id": task_id}) or {}).get("owner") == instance_id()
            # The lifespan has run its shutdown: the record says how the task ended.
            stopped = raw.find_one({"id": task_id})
        finally:
            reader.close()

        assert stopped is not None
        assert stopped["status"] == TaskStatus.FAILED.value
        assert stopped["error"] == TASK_STOPPED_MESSAGE


class TestTaskRoutes:
    """Submit, GET, list and cancel through the engine's routes, for tasks on either replica."""

    async def test_routes_answer_for_the_engines_own_tasks_and_another_replicas(
        self, document_engine
    ):
        store_b = DocuStore(document_engine.store_uri, document_engine.store_database)
        b = DocumentTaskManager(store_b, owner="replica-b")
        try:
            with document_engine.start() as stack:

                async def call(method: str, path: str, **kwargs: Any) -> Any:
                    send = getattr(stack.client, method)
                    return await asyncio.to_thread(send, path, headers=stack.headers, **kwargs)

                async def until_status(task_id: str, status: TaskStatus) -> dict:
                    deadline = time.monotonic() + 10.0
                    body: dict = {}
                    while time.monotonic() < deadline:
                        got = await call("get", f"/api/v1/tasks/{task_id}")
                        body = got.json() if got.status_code == 200 else {"http": got.status_code}
                        if body.get("status") == status.value:
                            return body
                        await asyncio.sleep(0.1)
                    raise AssertionError(f"{task_id} never read {status.value}, last {body}")

                # The engine's own task: submitted, read and cancelled through its routes.
                started = await call("post", STREAM_ROUTE, json=LONG_STREAM)
                assert started.status_code == 200, started.text
                own = started.json()["task_id"]
                await until_status(own, TaskStatus.RUNNING)
                seen_on_b = b.get(own)
                assert seen_on_b is not None
                assert seen_on_b.status is TaskStatus.RUNNING

                cancelled = await call("post", f"/api/v1/tasks/{own}/cancel")
                assert cancelled.status_code == 200, cancelled.text
                await until_status(own, TaskStatus.CANCELLED)

                # Another replica's task: read, listed and cancelled through these routes.
                loop = asyncio.get_running_loop()
                cancelled_at: list[float] = []

                async def until_cancelled(*, task: Any) -> None:
                    try:
                        await asyncio.Event().wait()
                    except asyncio.CancelledError:
                        cancelled_at.append(loop.time())
                        raise

                remote = b.submit("test:remote", until_cancelled)
                await until_status(remote.id, TaskStatus.RUNNING)
                listed = await call("get", "/api/v1/tasks", params={"kind": "test:remote"})
                assert [t["id"] for t in listed.json()] == [remote.id]

                asked_at = loop.time()
                asked = await call("post", f"/api/v1/tasks/{remote.id}/cancel")
                assert asked.status_code == 200, asked.text
                deadline = time.monotonic() + 5.0
                while not cancelled_at and time.monotonic() < deadline:
                    await asyncio.sleep(0.05)
                assert cancelled_at, "the cancel never reached replica B"
                assert cancelled_at[0] - asked_at <= 2.0, cancelled_at[0] - asked_at
                await until_status(remote.id, TaskStatus.CANCELLED)
        finally:
            await b.aclose()
            store_b.close()
