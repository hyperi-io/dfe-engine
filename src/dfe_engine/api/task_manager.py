#  Project:      dfe-engine
#  File:         src/dfe_engine/api/task_manager.py
#  Purpose:      In-memory async task manager with polling and SSE support
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""In-memory async task manager.

Provides fire-and-forget task execution with status polling and SSE streaming.
Tasks are ephemeral -- lost on restart. This is intentional: dfe-engine is a
control plane, not a job scheduler. If it restarts, pending tasks simply need
to be re-submitted.

A task can be held to a set of orgs at submit. A reader held to its own orgs then
sees only the tasks held to a subset of them, and the hold is evicted with the task.

Usage::

    manager = TaskManager()
    task = manager.submit("hunt:execute", coro_fn, arg1, arg2)
    status = manager.get(task.id)
    manager.cancel(task.id)
"""

import asyncio
import builtins
import json
import uuid
from collections.abc import Iterable
from datetime import UTC, date, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field
from scalo.logger import logger

TASK_FAILED_MESSAGE = "The task failed; the engine log has the reason"


def _json_safe(value: Any) -> Any:
    """Coerce task results so TaskInfo always serializes for OpenAPI responses."""
    if value is None:
        return None
    if isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):  # nan/inf
            return str(value)
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)


def _held(org_ids: Iterable[str] | None) -> frozenset[str] | None:
    """The orgs as a set, keeping None, which means every org."""
    return None if org_ids is None else frozenset(org_ids)


class TaskStatus(str, Enum):
    """Task lifecycle states."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        """True once the task can no longer change state (settled)."""
        return self in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED)


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

    def __init__(self, task_id: str, kind: str, held_to: frozenset[str] | None = None) -> None:
        self.id = task_id
        self.kind = kind
        self.held_to = held_to
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

    def visible_to(self, reader_orgs: frozenset[str] | None) -> bool:
        """Whether a reader held to ``reader_orgs`` may see this task; None sees every task."""
        if reader_orgs is None:
            return True
        return self.held_to is not None and self.held_to <= reader_orgs

    def to_info(self) -> TaskInfo:
        payload = {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "progress": max(0, min(100, int(self.progress))),
            "message": self.message,
            "result": _json_safe(self.result),
            "error": self.error,
        }
        # Round-trip so FastAPI response validation never sees non-JSON types.
        payload = json.loads(json.dumps(payload, default=str))
        return TaskInfo.model_validate(payload)

    def set_progress(self, progress: int, message: str = "") -> None:
        self.progress = min(progress, 100)
        if message:
            self.message = message
        self._progress_event.set()
        self._progress_event.clear()


class TaskManager:
    """In-memory async task manager.

    Not persistent -- tasks are lost on restart. Stores are bounded by
    ``max_completed`` to prevent unbounded memory growth.
    """

    def __init__(self, max_completed: int = 1000) -> None:
        self._tasks: dict[str, _Task] = {}
        self._max_completed = max_completed

    def submit(
        self,
        kind: str,
        coro_fn,
        *args,
        held_to: Iterable[str] | None = None,
        reportable: tuple[type[Exception], ...] = (),
        **kwargs,
    ) -> TaskInfo:
        """Submit an async callable for background execution.

        The coroutine function receives a ``task`` keyword argument -- a
        ``_Task`` instance whose ``set_progress()`` method can be called
        to report progress.

        Every failure is logged. Its message reaches the task's readers only when
        it is one of ``reportable``; any other failure reads as
        :data:`TASK_FAILED_MESSAGE`, because a backend's own error text can carry
        statement fragments, user names and password hashes.

        Args:
            kind: The task kind, for ``list(kind=...)``.
            coro_fn: The coroutine function to run, called with ``*args`` and ``**kwargs``.
            *args: Positional arguments for ``coro_fn``.
            held_to: The orgs the task's result is held to. None means it may hold
                any org's data, so only a reader of every org sees it.
            reportable: Exception types whose message the engine composes about the
                request, and which a reader may therefore see.
            **kwargs: Keyword arguments for ``coro_fn``.

        Returns:
            TaskInfo snapshot at submission time.
        """
        task_id = str(uuid.uuid4())
        task = _Task(task_id, kind, _held(held_to))
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
                logger.warning("background task failed", task_id=task.id, kind=kind, error=str(exc))
                task.status = TaskStatus.FAILED
                task.error = str(exc) if isinstance(exc, reportable) else TASK_FAILED_MESSAGE
                task.message = f"Failed: {task.error}"
            finally:
                task.completed_at = datetime.now(UTC).isoformat()
                task._progress_event.set()
                self._evict_completed()

        task.asyncio_task = asyncio.get_running_loop().create_task(_run())
        return task.to_info()

    def get(self, task_id: str, *, reader_orgs: Iterable[str] | None = None) -> TaskInfo | None:
        """Get current task state, or None if not found or not visible to the reader.

        Args:
            task_id: The task's id.
            reader_orgs: The orgs the reader is held to, None for a reader of every
                org. A held reader sees only a task held to a subset of its orgs.

        Returns:
            The task's TaskInfo, or None.
        """
        task = self._tasks.get(task_id)
        if task is None or not task.visible_to(_held(reader_orgs)):
            return None
        return task.to_info()

    def list(
        self, kind: str | None = None, *, reader_orgs: Iterable[str] | None = None
    ) -> builtins.list[TaskInfo]:
        """List the tasks visible to the reader, most recent first, optionally filtered by kind.

        Args:
            kind: Only tasks of this kind; None or empty for every kind.
            reader_orgs: As for :meth:`get`.

        Returns:
            The visible tasks' TaskInfo.
        """
        held = _held(reader_orgs)
        tasks = [
            t for t in self._tasks.values() if (not kind or t.kind == kind) and t.visible_to(held)
        ]
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

    async def await_terminal(self, task_id: str, wait: float) -> TaskInfo | None:
        """Block until the task is terminal or ``wait`` seconds elapse.

        The submit -> poll convenience the synchronous API paths share (sigma sync,
        sampler): returns the task's TaskInfo - terminal if it settled within the
        window, else the latest in-flight snapshot - or None for an unknown id.
        Waits on the task's own progress SIGNAL, not a poll timer (readiness-signal,
        not a raced timeout).
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait
        while True:
            info = self.get(task_id)
            if info is None or info.status.is_terminal:
                return info
            remaining = deadline - loop.time()
            if remaining <= 0:
                return info
            await self.wait_for_progress(task_id, timeout=remaining)

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
