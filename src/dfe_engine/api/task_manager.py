#  Project:      dfe-engine
#  File:         src/dfe_engine/api/task_manager.py
#  Purpose:      In-memory async task manager with polling and SSE support
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""In-memory async task manager.

Provides fire-and-forget task execution with status polling and SSE streaming.
Tasks are ephemeral — lost on restart. This is intentional: dfe-engine is a
control plane, not a job scheduler. If it restarts, pending tasks simply need
to be re-submitted.

Usage::

    manager = TaskManager()
    task = manager.submit("hunt:execute", coro_fn, arg1, arg2)
    status = manager.get(task.id)
    manager.cancel(task.id)
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


def _json_safe(value: Any) -> Any:
    """Coerce task results so TaskInfo always serializes for OpenAPI responses."""
    if value is None:
        return None
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)


class TaskStatus(str, Enum):
    """Task lifecycle states."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskInfo(BaseModel):
    """Public view of a task."""

    id: str = Field(description="Unique task ID (UUID)")
    kind: str = Field(description="Task kind (e.g. 'hunt:execute', 'pipeline:generate')")
    status: TaskStatus
    created_at: str = Field(description="ISO 8601 creation time")
    started_at: str | None = None
    completed_at: str | None = None
    progress: int = Field(default=0, ge=0, le=100, description="Progress percentage")
    message: str = Field(default="", description="Human-readable status message")
    result: Any | None = Field(default=None, description="Task result (on completion)")
    error: str | None = Field(default=None, description="Error message (on failure)")


class _Task:
    """Internal mutable task state."""

    def __init__(self, task_id: str, kind: str) -> None:
        self.id = task_id
        self.kind = kind
        self.status = TaskStatus.PENDING
        self.created_at = datetime.now(UTC).isoformat()
        self.started_at: str | None = None
        self.completed_at: str | None = None
        self.progress = 0
        self.message = ""
        self.result: Any | None = None
        self.error: str | None = None
        self.asyncio_task: asyncio.Task | None = None
        self._progress_event = asyncio.Event()

    def to_info(self) -> TaskInfo:
        return TaskInfo(
            id=self.id,
            kind=self.kind,
            status=self.status,
            created_at=self.created_at,
            started_at=self.started_at,
            completed_at=self.completed_at,
            progress=self.progress,
            message=self.message,
            result=_json_safe(self.result),
            error=self.error,
        )

    def set_progress(self, progress: int, message: str = "") -> None:
        self.progress = min(progress, 100)
        if message:
            self.message = message
        self._progress_event.set()
        self._progress_event.clear()


class TaskManager:
    """In-memory async task manager.

    Not persistent — tasks are lost on restart. Stores are bounded by
    ``max_completed`` to prevent unbounded memory growth.
    """

    def __init__(self, max_completed: int = 1000) -> None:
        self._tasks: dict[str, _Task] = {}
        self._max_completed = max_completed

    def submit(self, kind: str, coro_fn, *args, **kwargs) -> TaskInfo:
        """Submit an async callable for background execution.

        The coroutine function receives a ``task`` keyword argument — a
        ``_Task`` instance whose ``set_progress()`` method can be called
        to report progress.

        Returns:
            TaskInfo snapshot at submission time.
        """
        task_id = str(uuid.uuid4())
        task = _Task(task_id, kind)
        self._tasks[task_id] = task

        async def _run() -> None:
            task.status = TaskStatus.RUNNING
            task.started_at = datetime.now(UTC).isoformat()
            task.message = "Running"
            try:
                result = await coro_fn(*args, task=task, **kwargs)
                task.status = TaskStatus.COMPLETED
                task.progress = 100
                task.result = result
                task.message = "Completed"
            except asyncio.CancelledError:
                task.status = TaskStatus.CANCELLED
                task.message = "Cancelled"
            except Exception as exc:
                task.status = TaskStatus.FAILED
                task.error = str(exc)
                task.message = f"Failed: {exc}"
            finally:
                task.completed_at = datetime.now(UTC).isoformat()
                task._progress_event.set()
                self._evict_completed()

        task.asyncio_task = asyncio.get_running_loop().create_task(_run())
        return task.to_info()

    def get(self, task_id: str) -> TaskInfo | None:
        """Get current task state, or None if not found."""
        task = self._tasks.get(task_id)
        return task.to_info() if task else None

    def list(self, kind: str | None = None) -> list[TaskInfo]:
        """List all tasks, optionally filtered by kind."""
        tasks = self._tasks.values()
        if kind:
            tasks = [t for t in tasks if t.kind == kind]
        return [t.to_info() for t in sorted(tasks, key=lambda t: t.created_at, reverse=True)]

    def cancel(self, task_id: str) -> bool:
        """Cancel a running task. Returns True if cancellation was requested."""
        task = self._tasks.get(task_id)
        if task is None:
            return False
        if task.asyncio_task and not task.asyncio_task.done():
            task.asyncio_task.cancel()
            return True
        return False

    async def wait_for_progress(self, task_id: str, timeout: float = 30.0) -> TaskInfo | None:
        """Block until the task reports progress or completes. For SSE polling."""
        task = self._tasks.get(task_id)
        if task is None:
            return None
        try:
            await asyncio.wait_for(task._progress_event.wait(), timeout=timeout)
        except TimeoutError:
            pass
        return task.to_info()

    def _evict_completed(self) -> None:
        """Remove oldest completed/failed/cancelled tasks beyond max_completed."""
        finished = [
            t
            for t in self._tasks.values()
            if t.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED)
        ]
        if len(finished) <= self._max_completed:
            return
        finished.sort(key=lambda t: t.completed_at or "")
        for t in finished[: len(finished) - self._max_completed]:
            self._tasks.pop(t.id, None)
