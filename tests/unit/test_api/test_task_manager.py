#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_task_manager.py
#  Purpose:      Tests for in-memory async task manager
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Tests for TaskManager — in-memory async task lifecycle."""

from __future__ import annotations

import asyncio
import json

import pytest

from dfe_engine.api.task_manager import TaskManager, TaskStatus


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
    async def test_task_failure_records_error(self, manager: TaskManager):
        async def fail(*, task):
            raise ValueError("boom")

        info = manager.submit("test:fail", fail)
        await asyncio.sleep(0.1)

        result = manager.get(info.id)
        assert result is not None
        assert result.status == TaskStatus.FAILED
        assert "boom" in result.error

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
        manager.submit("sampler:sample", noop)
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

        # Eviction runs on completion — should have at most max_completed finished tasks
        tasks = manager.list()
        assert len(tasks) <= 5


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
