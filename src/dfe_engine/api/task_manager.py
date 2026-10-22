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

This manager answers only for the tasks its own process runs, which is right for one
engine. :class:`dfe_engine.api.document_tasks.DocumentTaskManager` keeps them in the
document store, so every replica answers for every task.

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
TASK_STOPPED_MESSAGE = "The engine running the task stopped before it finished"
TASK_RESULT_TOO_LARGE = "The task finished, but its result is too large to keep"
CLOSE_TIMEOUT_SECONDS = 5.0
"""How long :meth:`TaskManager.aclose` waits for cancelled tasks to record how they ended."""
# Half the 16 MiB document the store takes, so the record holding the largest result fits.
MAX_RESULT_BYTES = 8 * 1024 * 1024


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


def _result_size(value: Any) -> int:
    """The bytes ``value`` takes as the JSON a task result is kept as, ASCII-escaped."""
    return len(json.dumps(_json_safe(value), default=str))


def _held(org_ids: Iterable[str] | None) -> frozenset[str] | None:
    """The orgs as a set, keeping None, which means every org."""
    return None if org_ids is None else frozenset(org_ids)


def _visible(held_to: frozenset[str] | None, reader_orgs: frozenset[str] | None) -> bool:
    """Whether a reader held to ``reader_orgs`` sees a task held to ``held_to``. None sees all."""
    if reader_orgs is None:
        return True
    return held_to is not None and held_to <= reader_orgs


class TaskLimitError(RuntimeError):
    """Every task a kind may run at once is already pending or running.

    Attributes:
        kind: The task kind.
        limit: How many tasks of the kind may be pending or running at once.
    """

    def __init__(self, kind: str, limit: int) -> None:
        super().__init__(f"{limit} {kind} tasks are already pending or running")
        self.kind = kind
        self.limit = limit


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
        # Moved by every set_progress, so a store can tell a report it has not written yet.
        self.revision = 0
        self.cancel_sent = False
        self._progress_event = asyncio.Event()

    def visible_to(self, reader_orgs: frozenset[str] | None) -> bool:
        """Whether a reader held to ``reader_orgs`` may see this task. None sees every task."""
        return _visible(self.held_to, reader_orgs)

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
        self.revision += 1
        self._progress_event.set()
        self._progress_event.clear()

    async def wait_changed(self, timeout: float) -> None:
        """Return when the task reports progress or ends, or after ``timeout`` seconds."""
        try:
            await asyncio.wait_for(self._progress_event.wait(), timeout=timeout)
        except TimeoutError:
            pass


class TaskManager:
    """In-memory async task manager.

    Not persistent -- tasks are lost on restart. Stores are bounded by
    ``max_completed`` to prevent unbounded memory growth.
    """

    def __init__(self, max_completed: int = 1000) -> None:
        self._tasks: dict[str, _Task] = {}
        self._max_completed = max_completed
        self._closing = False

    def submit(
        self,
        kind: str,
        coro_fn,
        *args,
        held_to: Iterable[str] | None = None,
        reportable: tuple[type[Exception], ...] = (),
        max_active: int | None = None,
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

        A result over :data:`MAX_RESULT_BYTES` as JSON is not kept, and the task fails
        with :data:`TASK_RESULT_TOO_LARGE`, so one engine and a cluster agree.

        Args:
            kind: The task kind, for ``list(kind=...)``.
            coro_fn: The coroutine function to run, called with ``*args`` and ``**kwargs``.
            *args: Positional arguments for ``coro_fn``.
            held_to: The orgs the task's result is held to. None means it may hold
                any org's data, so only a reader of every org sees it.
            reportable: Exception types whose message the engine composes about the
                request, and which a reader may therefore see.
            max_active: How many tasks of ``kind`` may be pending or running at once,
                this one included. None for no limit.
            **kwargs: Keyword arguments for ``coro_fn``.

        Returns:
            TaskInfo snapshot at submission time.

        Raises:
            TaskLimitError: ``max_active`` tasks of ``kind`` are already pending or running.
        """
        task_id = str(uuid.uuid4())
        task = _Task(task_id, kind, _held(held_to))
        self._admit(task, max_active)
        self._tasks[task_id] = task

        async def _run() -> None:
            try:
                task.status = TaskStatus.RUNNING
                task.started_at = datetime.now(UTC).isoformat()
                task.message = "Running"
                await self._record(task)
                result = await coro_fn(*args, task=task, **kwargs)
                size = _result_size(result)
                if size > MAX_RESULT_BYTES:
                    logger.warning(
                        "background task result too large to keep",
                        task_id=task.id,
                        kind=kind,
                        size_bytes=size,
                        limit_bytes=MAX_RESULT_BYTES,
                    )
                    task.status = TaskStatus.FAILED
                    task.error = TASK_RESULT_TOO_LARGE
                    task.message = f"Failed: {TASK_RESULT_TOO_LARGE}"
                else:
                    task.status = TaskStatus.COMPLETED
                    task.progress = 100
                    task.result = result
                    task.message = "Completed"
            except asyncio.CancelledError:
                self._end_cancelled(task)
            except Exception as exc:
                logger.warning("background task failed", task_id=task.id, kind=kind, error=str(exc))
                task.status = TaskStatus.FAILED
                task.error = str(exc) if isinstance(exc, reportable) else TASK_FAILED_MESSAGE
                task.message = f"Failed: {task.error}"
            finally:
                task.completed_at = datetime.now(UTC).isoformat()
                await self._record(task)
                task._progress_event.set()
                self._evict_completed()

        def _settle_unstarted(_: asyncio.Task) -> None:
            # A task cancelled before its first step never enters _run, so nothing else ends it.
            if task.status is not TaskStatus.PENDING:
                return
            self._end_cancelled(task)
            task.completed_at = datetime.now(UTC).isoformat()
            task._progress_event.set()
            self._evict_completed()

        task.asyncio_task = asyncio.get_running_loop().create_task(_run())
        task.asyncio_task.add_done_callback(_settle_unstarted)
        self._submitted(task)
        return task.to_info()

    def _admit(self, task: _Task, max_active: int | None) -> None:
        """Refuse ``task`` when ``max_active`` tasks of its kind are pending or running here.

        Raises:
            TaskLimitError: The limit is reached.
        """
        if max_active is None:
            return
        active = sum(
            1 for t in self._tasks.values() if t.kind == task.kind and not t.status.is_terminal
        )
        if active >= max_active:
            raise TaskLimitError(task.kind, max_active)

    def _submitted(self, task: _Task) -> None:
        """Called once ``task`` is running in the background. Nothing more to do in memory."""

    async def _record(self, task: _Task) -> None:
        """Keep ``task``'s state where readers find it. In memory it already is."""

    def _end_cancelled(self, task: _Task) -> None:
        """Mark ``task`` ended by a cancel: one asked for, or the engine stopping."""
        if self._closing:
            task.status = TaskStatus.FAILED
            task.error = TASK_STOPPED_MESSAGE
            task.message = f"Failed: {TASK_STOPPED_MESSAGE}"
        else:
            task.status = TaskStatus.CANCELLED
            task.message = "Cancelled"

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
        return self._cancel_local(task)

    def _cancel_local(self, task: _Task) -> bool:
        """Cancel the coroutine running ``task`` here, once. True while it has not ended."""
        if task.status.is_terminal or task.asyncio_task is None or task.asyncio_task.done():
            return False
        # A second cancel would land in _run's finally and stop it recording how the task ended.
        if not task.cancel_sent:
            task.cancel_sent = True
            task.asyncio_task.cancel()
        return True

    async def aclose(self) -> None:
        """Stop every task this process runs, each recorded as stopped by the engine.

        Waits up to :data:`CLOSE_TIMEOUT_SECONDS` for them to record it.
        """
        self._closing = True
        running: builtins.list[asyncio.Task] = []
        for task in builtins.list(self._tasks.values()):
            if self._cancel_local(task) and task.asyncio_task is not None:
                running.append(task.asyncio_task)
        if running:
            await asyncio.wait(running, timeout=CLOSE_TIMEOUT_SECONDS)

    async def wait_for_progress(self, task_id: str, timeout: float = 30.0) -> TaskInfo | None:
        """Block until the task reports progress or completes. For SSE polling."""
        task = self._tasks.get(task_id)
        if task is None:
            return None
        await task.wait_changed(timeout)
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
