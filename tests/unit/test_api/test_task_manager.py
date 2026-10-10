#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_task_manager.py
#  Purpose:      Tests for in-memory async task manager
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Tests for TaskManager -- in-memory async task lifecycle."""

import asyncio
import json

import pytest

from dfe_engine.api.task_manager import TASK_FAILED_MESSAGE, TaskManager, TaskStatus


@pytest.fixture
def manager() -> TaskManager:
    return TaskManager(max_completed=5)


class TestTaskSubmitAndGet:
    @pytest.mark.asyncio
    async def test_submit_returns_pending_info(self, manager: TaskManager):
        async def noop(*, task):
            return "done"

        info = manager.submit("test:noop", noop)
        assert info.kind == "test:noop"
        assert info.id

    @pytest.mark.asyncio
    async def test_task_completes_with_result(self, manager: TaskManager):
        async def compute(*, task):
            return {"answer": 42}

        info = manager.submit("test:compute", compute)
        # Wait briefly for task to complete
        await asyncio.sleep(0.1)

        result = manager.get(info.id)
        assert result is not None
        assert result.status == TaskStatus.COMPLETED
        assert result.result == {"answer": 42}
        assert result.progress == 100

    @pytest.mark.asyncio
    async def test_non_json_serializable_result_is_coerced(self, manager: TaskManager):
        from datetime import UTC, datetime
        from pathlib import Path

        async def weird_result(*, task):
            return {"path": Path("/tmp"), "at": datetime.now(UTC)}

        info = manager.submit("test:weird", weird_result)
        await asyncio.sleep(0.1)

        result = manager.get(info.id)
        assert result is not None
        assert result.status == TaskStatus.COMPLETED
        json.dumps(result.model_dump(mode="json"))

    @pytest.mark.asyncio
    async def test_a_reportable_failure_keeps_its_message(self, manager: TaskManager):
        async def fail(*, task):
            raise ValueError("boom")

        info = manager.submit("test:fail", fail, reportable=(ValueError,))
        await asyncio.sleep(0.1)

        result = manager.get(info.id)
        assert result is not None
        assert result.status == TaskStatus.FAILED
        assert result.error == "boom"
        assert result.message == "Failed: boom"

    @pytest.mark.asyncio
    async def test_any_other_failure_reads_generic_and_goes_to_the_log(
        self, manager: TaskManager, audit_events: list[dict]
    ):
        async def fail(*, task):
            raise RuntimeError("ALTER USER x IDENTIFIED WITH sha256_hash BY 'deadbeef'")

        info = manager.submit("test:fail", fail, reportable=(ValueError,))
        await asyncio.sleep(0.1)

        result = manager.get(info.id)
        assert result is not None
        assert result.status == TaskStatus.FAILED
        assert result.error == TASK_FAILED_MESSAGE
        assert "IDENTIFIED" not in result.message
        assert any("deadbeef" in str(event.get("error", "")) for event in audit_events)

    @pytest.mark.asyncio
    async def test_task_cancellation(self, manager: TaskManager):
        async def slow(*, task):
            await asyncio.sleep(10)

        info = manager.submit("test:slow", slow)
        await asyncio.sleep(0.05)

        cancelled = manager.cancel(info.id)
        assert cancelled is True
        await asyncio.sleep(0.1)

        result = manager.get(info.id)
        assert result is not None
        assert result.status == TaskStatus.CANCELLED

    @pytest.mark.asyncio
    async def test_cancel_nonexistent_returns_false(self, manager: TaskManager):
        assert manager.cancel("nonexistent") is False

    @pytest.mark.asyncio
    async def test_get_nonexistent_returns_none(self, manager: TaskManager):
        assert manager.get("nonexistent") is None


class TestTaskList:
    @pytest.mark.asyncio
    async def test_list_all_tasks(self, manager: TaskManager):
        async def noop(*, task):
            return "ok"

        manager.submit("a:task", noop)
        manager.submit("b:task", noop)
        await asyncio.sleep(0.1)

        tasks = manager.list()
        assert len(tasks) == 2

    @pytest.mark.asyncio
    async def test_list_filtered_by_kind(self, manager: TaskManager):
        async def noop(*, task):
            return "ok"

        manager.submit("hunt:execute", noop)
        manager.submit("pipeline:build", noop)
        await asyncio.sleep(0.1)

        hunts = manager.list(kind="hunt:execute")
        assert len(hunts) == 1
        assert hunts[0].kind == "hunt:execute"


class TestTaskProgress:
    @pytest.mark.asyncio
    async def test_progress_updates(self, manager: TaskManager):
        async def with_progress(*, task):
            task.set_progress(50, "halfway")
            await asyncio.sleep(0.05)
            return "done"

        info = manager.submit("test:progress", with_progress)
        await asyncio.sleep(0.2)

        result = manager.get(info.id)
        assert result is not None
        assert result.status == TaskStatus.COMPLETED
        assert result.progress == 100


class TestEviction:
    @pytest.mark.asyncio
    async def test_evicts_oldest_completed(self, manager: TaskManager):
        async def noop(*, task):
            return "ok"

        # Submit more than max_completed (5)
        for _ in range(8):
            manager.submit("test:evict", noop)

        await asyncio.sleep(0.3)

        # Eviction runs on completion -- should have at most max_completed finished tasks
        tasks = manager.list()
        assert len(tasks) <= 5


async def _settled(manager: TaskManager, held_to: list[str] | None) -> str:
    """Submit a task held to ``held_to``, wait for it to settle, and return its id."""

    async def noop(*, task):
        return "ok"

    info = manager.submit("test:held", noop, held_to=held_to)
    settled = await manager.await_terminal(info.id, 2.0)
    assert settled is not None
    assert settled.status == TaskStatus.COMPLETED
    return info.id


class TestHeldTasks:
    @pytest.mark.asyncio
    async def test_a_held_reader_sees_only_tasks_held_to_its_orgs(self, manager: TaskManager):
        own = await _settled(manager, ["org-a"])
        wider = await _settled(manager, ["org-a", "org-b"])
        every_org = await _settled(manager, None)

        assert manager.get(own, reader_orgs=["org-a", "org-c"]) is not None
        assert manager.get(wider, reader_orgs=["org-a"]) is None
        assert manager.get(every_org, reader_orgs=["org-a", "org-b"]) is None
        assert [t.id for t in manager.list(reader_orgs=["org-a", "org-b"])] == [wider, own]
        assert {t.id for t in manager.list()} == {own, wider, every_org}
        assert manager.get(every_org) is not None

    @pytest.mark.asyncio
    async def test_a_reader_held_to_no_org_sees_no_task(self, manager: TaskManager):
        held = await _settled(manager, ["org-a"])
        await _settled(manager, None)

        assert manager.get(held, reader_orgs=[]) is None
        assert manager.list(reader_orgs=[]) == []

    @pytest.mark.asyncio
    async def test_eviction_takes_the_scope_with_the_task(self):
        manager = TaskManager(max_completed=2)
        first = await _settled(manager, ["org-a"])
        kept = [await _settled(manager, ["org-a"]) for _ in range(2)]

        assert manager.get(first) is None
        assert manager.get(first, reader_orgs=["org-a"]) is None
        assert manager.list(reader_orgs=["org-a"]) == manager.list()
        assert {t.id for t in manager.list(reader_orgs=["org-a"])} == set(kept)
        assert [t.held_to for t in manager._tasks.values()] == [frozenset({"org-a"})] * 2


class TestWaitForProgress:
    @pytest.mark.asyncio
    async def test_wait_returns_on_completion(self, manager: TaskManager):
        async def quick(*, task):
            return "fast"

        info = manager.submit("test:quick", quick)
        result = await manager.wait_for_progress(info.id, timeout=2.0)
        assert result is not None
        assert result.status in (TaskStatus.COMPLETED, TaskStatus.RUNNING)

    @pytest.mark.asyncio
    async def test_wait_nonexistent_returns_none(self, manager: TaskManager):
        result = await manager.wait_for_progress("nonexistent", timeout=0.1)
        assert result is None

    @pytest.mark.asyncio
    async def test_wait_timeout_returns_current_state(self, manager: TaskManager):
        async def slow(*, task):
            await asyncio.sleep(10)

        info = manager.submit("test:slow", slow)
        result = await manager.wait_for_progress(info.id, timeout=0.1)
        assert result is not None
        # Task is still running after short timeout
        assert result.status in (TaskStatus.RUNNING, TaskStatus.PENDING)
        manager.cancel(info.id)
