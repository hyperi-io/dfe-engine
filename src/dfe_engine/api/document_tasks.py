#  Project:      dfe-engine
#  File:         api/document_tasks.py
#  Purpose:      Task status in the document store, so every engine replica answers for every task
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Task status in the document store, so every engine replica answers for every task.

The replica a task is submitted to owns it: it runs the coroutine, writes each change to
the task's record, and beats the record's heartbeat while the task runs. Any replica
reads and lists tasks and asks for a cancel, which the owner reads within
:data:`WATCH_SECONDS` and acts on. A task still running on a heartbeat older than
:data:`OWNER_LOST_SECONDS` has lost its owner. It reads as failed, and the first replica
to read it records that, so an owner that comes back cannot write over it.

A kind submitted with ``max_active`` takes one of that many claims in
:data:`SLOTS_COLLECTION`, so the limit counts every replica's tasks. A claim held by a
task that has ended, or lost its owner, is taken back by the next submit that needs it.
"""

import asyncio
import builtins
import json
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from bson.errors import InvalidDocument
from pydantic import BaseModel, ValidationError
from pymongo import DESCENDING
from pymongo.errors import PyMongoError
from scalo.concurrency import run_blocking
from scalo.logger import logger

from dfe_engine.api.task_manager import (
    TASK_STOPPED_MESSAGE,
    TaskInfo,
    TaskLimitError,
    TaskManager,
    TaskStatus,
    _held,
    _Task,
    _visible,
)
from dfe_engine.replicas import instance_id
from dfe_engine.store.documents import REV, DocumentConflictError, DocuStore

TASKS_COLLECTION = "tasks"
SLOTS_COLLECTION = "task_slots"

# Every replica judges a heartbeat against the same window, so it is not a setting.
OWNER_LOST_SECONDS = 30.0
# Six beats inside the window, so one slow write or a short store outage loses no task.
HEARTBEAT_SECONDS = 5.0
# How often an owner reads for cancels and writes progress, so a cancel lands within two.
WATCH_SECONDS = 1.0

TASK_RESULT_TOO_LARGE = "The task finished, but its result is too large to keep"

_ENDED = [status.value for status in TaskStatus if status.is_terminal]


class TaskRecord(BaseModel):
    """One task as the document store keeps it.

    Attributes:
        id: The task id.
        kind: The task kind.
        owner: The instance id of the replica running it.
        held_to: The orgs its result is held to, sorted. None for every org.
        status: Where it is in its lifecycle.
        created_at: ISO 8601 creation time.
        started_at: ISO 8601 start time.
        completed_at: ISO 8601 end time.
        progress: Percentage done.
        message: Human-readable status.
        result: The result as JSON text, so its keys need not suit the store.
        error: Why it failed.
        heartbeat_at: When the owner last wrote the record, in seconds since the epoch.
        cancel_requested: A replica other than the owner asked for a cancel.
    """

    id: str
    kind: str
    owner: str
    held_to: list[str] | None = None
    status: TaskStatus = TaskStatus.PENDING
    created_at: str
    started_at: str | None = None
    completed_at: str | None = None
    progress: int = 0
    message: str = ""
    result: str | None = None
    error: str | None = None
    heartbeat_at: float
    cancel_requested: bool = False


class _UnwritableError(Exception):
    """The record has ended or belongs to another owner, so the write does not apply."""


class _Outcome(StrEnum):
    """What became of one owner write."""

    WRITTEN = "written"
    LOST = "lost"
    """Another replica ended the record, or it is gone."""
    FAILED = "failed"
    """The store refused the write or could not be reached, so the next watch tries again."""


@dataclass(frozen=True, slots=True)
class _Write:
    """One owner write, taken on the event loop and made on a worker thread."""

    task_id: str
    fields: dict[str, Any]
    revision: int
    slot: str | None
    ends: bool


@dataclass(slots=True)
class _Held:
    """What the owner knows about writing one of its tasks."""

    slot: str | None = None
    written: int = 0
    beat_at: float = 0.0
    done: bool = False
    """Nothing is left to write: the end is recorded, or another replica ended the task."""


def _skip_invalid(doc: dict, exc: ValidationError) -> None:
    logger.warning(
        "task record skipped: it is not one this engine reads",
        task_id=str(doc.get("id", "")),
        error=str(exc),
    )


def _from_stored(doc: dict) -> TaskRecord | None:
    """The record in a raw stored document, or None for one this engine cannot read."""
    doc.pop("_id", None)
    doc.pop(REV, None)
    try:
        return TaskRecord.model_validate(doc)
    except ValidationError as exc:
        _skip_invalid(doc, exc)
        return None


def _info(record: TaskRecord) -> TaskInfo:
    return TaskInfo(
        id=record.id,
        kind=record.kind,
        status=record.status,
        created_at=record.created_at,
        started_at=record.started_at,
        completed_at=record.completed_at,
        progress=max(0, min(100, record.progress)),
        message=record.message,
        result=None if record.result is None else json.loads(record.result),
        error=record.error,
    )


def _fields(task: _Task) -> dict[str, Any]:
    """The record fields the owner writes from its live ``task``."""
    info = task.to_info()
    return {
        "status": info.status,
        "started_at": info.started_at,
        "completed_at": info.completed_at,
        "progress": info.progress,
        "message": info.message,
        "result": None if info.result is None else json.dumps(info.result),
        "error": info.error,
    }


def _lost(record: TaskRecord, now: float) -> bool:
    """Whether ``record`` still runs on a heartbeat its owner stopped writing."""
    return not record.status.is_terminal and now - record.heartbeat_at > OWNER_LOST_SECONDS


def _failed_for_lost_owner(record: TaskRecord, now: float) -> TaskRecord:
    return record.model_copy(
        update={
            "status": TaskStatus.FAILED,
            "error": TASK_STOPPED_MESSAGE,
            "message": f"Failed: {TASK_STOPPED_MESSAGE}",
            "completed_at": datetime.fromtimestamp(now, UTC).isoformat(),
        }
    )


def _result_dropped(fields: dict[str, Any]) -> dict[str, Any]:
    return fields | {
        "status": TaskStatus.FAILED,
        "result": None,
        "error": TASK_RESULT_TOO_LARGE,
        "message": f"Failed: {TASK_RESULT_TOO_LARGE}",
    }


def _cancel_requested(record: TaskRecord) -> TaskRecord:
    """``record`` asking its owner for a cancel. One already asking writes nothing."""
    if record.status.is_terminal:
        raise _UnwritableError(record.id)
    if record.cancel_requested:
        return record
    return record.model_copy(update={"cancel_requested": True})


class DocumentTaskManager(TaskManager):
    """Tasks in the document store, run by the replica they were submitted to.

    Reads, lists and cancels go to the store, so every replica sees every task. This
    replica's own tasks keep their coroutine and live progress in memory until the
    store holds how each ended.
    """

    def __init__(
        self, store: DocuStore, *, owner: str | None = None, max_completed: int = 1000
    ) -> None:
        """Keep tasks in ``store``, running this replica's own as ``owner``.

        Args:
            store: The document store every replica shares.
            owner: This replica's id in the records it owns. Defaults to the process's
                instance id.
            max_completed: How many ended tasks the store keeps, across every replica.
        """
        super().__init__(max_completed=max_completed)
        self._owner = owner if owner is not None else instance_id()
        self._records = store.typed(
            TASKS_COLLECTION, TaskRecord, key="id", on_invalid=_skip_invalid
        )
        self._raw = store.collection(TASKS_COLLECTION)
        self._slots = store.claims(SLOTS_COLLECTION)
        self._held_by_id: dict[str, _Held] = {}
        # One owner write at a time, so a heartbeat never races the write that ends a task.
        self._writing = asyncio.Lock()
        self._watcher: asyncio.Task | None = None
        self._store_down = False

    # -- reads, from any replica ----------------------------------------------

    def get(self, task_id: str, *, reader_orgs: Iterable[str] | None = None) -> TaskInfo | None:
        """See base class. Reads the store, so it answers for every replica's tasks."""
        record = self._records.get(task_id)
        if record is None or not _visible(_held(record.held_to), _held(reader_orgs)):
            return None
        return _info(self._settle(record))

    def list(
        self, kind: str | None = None, *, reader_orgs: Iterable[str] | None = None
    ) -> builtins.list[TaskInfo]:
        """See base class. Reads the store, so it answers for every replica's tasks."""
        held = _held(reader_orgs)
        records = self._find({"kind": kind} if kind else {})
        return [_info(self._settle(r)) for r in records if _visible(_held(r.held_to), held)]

    def cancel(self, task_id: str) -> bool:
        """Cancel a running task, on whichever replica runs it.

        The owner's own call cancels at once. Any other replica marks the record, and
        the owner cancels within :data:`WATCH_SECONDS`.

        Returns:
            True when a cancel was requested of a task still running.
        """
        task = self._tasks.get(task_id)
        if task is not None:
            return self._cancel_local(task)
        record = self._records.get(task_id)
        if record is None or self._settle(record).status.is_terminal:
            return False
        try:
            self._records.mutate(task_id, _cancel_requested)
        except KeyError, _UnwritableError:
            return False
        return True

    async def wait_for_progress(self, task_id: str, timeout: float = 30.0) -> TaskInfo | None:
        """See base class. Reads the store each :data:`WATCH_SECONDS` until the task changes."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        first = await run_blocking(self.get, task_id)
        latest = first
        while latest is not None and not latest.status.is_terminal and latest == first:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            task = self._tasks.get(task_id)
            if task is not None:
                await task.wait_changed(min(remaining, WATCH_SECONDS))
            else:
                await asyncio.sleep(min(remaining, WATCH_SECONDS))
            latest = await run_blocking(self.get, task_id)
        return latest

    def _find(self, query: Mapping[str, Any]) -> builtins.list[TaskRecord]:
        """Every readable record matching ``query``, newest first."""
        found = self._raw.find(dict(query)).sort("created_at", DESCENDING)
        return [r for r in (_from_stored(doc) for doc in found) if r is not None]

    def _settle(self, record: TaskRecord) -> TaskRecord:
        """``record``, failed for a lost owner once its heartbeat runs out, and recorded so.

        The check is made again against the stored record, so an owner that beat in
        between keeps its task. A store that cannot take the write still reads the task
        as failed.
        """
        now = time.time()
        if not _lost(record, now):
            return record

        def fail_if_lost(stored: TaskRecord) -> TaskRecord:
            return _failed_for_lost_owner(stored, now) if _lost(stored, now) else stored

        try:
            return self._records.mutate(record.id, fail_if_lost)
        except KeyError:
            return _failed_for_lost_owner(record, now)
        except (PyMongoError, DocumentConflictError) as exc:
            logger.warning(
                "a lost task read as failed, not recorded", task_id=record.id, error=str(exc)
            )
            return _failed_for_lost_owner(record, now)

    # -- the owner's side -------------------------------------------------------

    def _admit(self, task: _Task, max_active: int | None) -> None:
        """Write ``task``'s record, then take a slot of its kind when ``max_active`` limits it.

        The record goes first, so a slot is never held by a task with no record to
        judge it by.

        Raises:
            TaskLimitError: Every slot of the kind is held by a task still running.
            DocumentConflictError: The store kept losing the write to concurrent ones.
        """
        now = time.time()
        held_to = sorted(task.held_to) if task.held_to is not None else None
        record = TaskRecord(
            id=task.id,
            kind=task.kind,
            owner=self._owner,
            held_to=held_to,
            created_at=task.created_at,
            heartbeat_at=now,
        )
        if not self._records.insert(record):
            raise DocumentConflictError(f"task id {task.id} is already taken")
        held = _Held(written=task.revision, beat_at=now)
        if max_active is not None:
            try:
                held.slot = self._take_slot(task.kind, max_active, task.id)
            except Exception:
                self._records.delete(task.id)
                raise
            if held.slot is None:
                self._records.delete(task.id)
                raise TaskLimitError(task.kind, max_active)
        self._held_by_id[task.id] = held

    def _take_slot(self, kind: str, limit: int, task_id: str) -> str | None:
        """Claim one of ``limit`` slots of ``kind`` for ``task_id``, or None when all are held."""
        for n in range(limit):
            key = f"{kind}#{n}"
            if self._slots.claim(key, task_id):
                return key
            holder = self._slots.holder(key)
            if holder is None or not self._has_ended(holder):
                continue
            self._slots.release(key, holder)
            if self._slots.claim(key, task_id):
                return key
        return None

    def _has_ended(self, task_id: str) -> bool:
        """Whether the task has ended, lost its owner, or has no record left."""
        record = self._records.get(task_id)
        return record is None or self._settle(record).status.is_terminal

    def _submitted(self, task: _Task) -> None:
        if self._watcher is None or self._watcher.done():
            self._watcher = asyncio.get_running_loop().create_task(
                self._watch(), name="task-owner-watch"
            )
            self._watcher.add_done_callback(_watcher_ended)

    async def _record(self, task: _Task) -> None:
        async with self._writing:
            write = self._write_for(task)
            if write is not None:
                flushed, error = await run_blocking(self._flush, write)
                self._apply(task, write, flushed, error, time.time())
            self._forget_if_done(task)

    def _evict_completed(self) -> None:
        """The store keeps its own bound: the write that ends a task prunes the oldest."""

    async def aclose(self) -> None:
        """See base class. Then records the end of any task cancelled before it started."""
        await super().aclose()
        if self._tasks:
            await self._watch_once()
        if self._watcher is not None:
            self._watcher.cancel()
            await asyncio.gather(self._watcher, return_exceptions=True)

    async def _watch(self) -> None:
        """Beat, write progress and act on cancels for this replica's tasks, until it has none."""
        while self._tasks:
            await asyncio.sleep(WATCH_SECONDS)
            await self._watch_once()

    async def _watch_once(self) -> None:
        async with self._writing:
            now = time.time()
            writes: builtins.list[tuple[_Task, _Write]] = []
            polled: builtins.list[str] = []
            for task in builtins.list(self._tasks.values()):
                held = self._held_by_id.get(task.id)
                write = self._write_for(task)
                if held is None or write is None:
                    continue
                if not task.status.is_terminal and not task.cancel_sent:
                    polled.append(task.id)
                stale = now - held.beat_at >= HEARTBEAT_SECONDS
                if write.ends or write.revision != held.written or stale:
                    writes.append((task, write))
            if not writes and not polled:
                return
            try:
                outcomes, cancels = await run_blocking(
                    self._watch_store, [w for _, w in writes], polled
                )
            except (PyMongoError, DocumentConflictError) as exc:
                self._note_store(str(exc))
                return
            for (task, write), (flushed, error) in zip(writes, outcomes, strict=True):
                self._apply(task, write, flushed, error, now)
                self._forget_if_done(task)
            for task_id in cancels:
                task = self._tasks.get(task_id)
                if task is not None:
                    self._cancel_local(task)

    def _write_for(self, task: _Task) -> _Write | None:
        """The write that brings ``task``'s record up to date, or None when nothing is left."""
        held = self._held_by_id.get(task.id)
        if held is None or held.done:
            return None
        return _Write(
            task_id=task.id,
            fields=_fields(task),
            revision=task.revision,
            slot=held.slot,
            ends=task.status.is_terminal,
        )

    def _apply(
        self, task: _Task, write: _Write, flushed: _Outcome, error: str | None, now: float
    ) -> None:
        """Note on the event loop what became of ``write``."""
        held = self._held_by_id.get(task.id)
        if held is None:
            return
        self._note_store(error)
        if flushed is _Outcome.WRITTEN:
            held.written = write.revision
            held.beat_at = now
            held.done = write.ends
        elif flushed is _Outcome.LOST:
            held.done = True
            if not task.status.is_terminal:
                logger.warning(
                    "task ended by another replica, so stopped here",
                    task_id=task.id,
                    kind=task.kind,
                )
                self._cancel_local(task)

    def _forget_if_done(self, task: _Task) -> None:
        held = self._held_by_id.get(task.id)
        if held is not None and held.done and task.status.is_terminal:
            self._tasks.pop(task.id, None)
            self._held_by_id.pop(task.id, None)

    def _note_store(self, error: str | None) -> None:
        """Log the store failing owner writes, and recovering, once each."""
        if error is not None and not self._store_down:
            logger.warning("task state not written, the owner retries each second", error=error)
        elif error is None and self._store_down:
            logger.info("task state written again")
        self._store_down = error is not None

    # -- worker-thread halves of the owner's writes ------------------------------

    def _watch_store(
        self, writes: builtins.list[_Write], polled: builtins.list[str]
    ) -> tuple[builtins.list[tuple[_Outcome, str | None]], set[str]]:
        """Make ``writes``, then read which of ``polled`` another replica asked to cancel."""
        outcomes = [self._flush(write) for write in writes]
        cancels: set[str] = set()
        if polled:
            found = self._raw.find(
                {"id": {"$in": polled}, "cancel_requested": True}, {"id": 1, "_id": 0}
            )
            cancels = {doc["id"] for doc in found}
        return outcomes, cancels

    def _flush(self, write: _Write) -> tuple[_Outcome, str | None]:
        """Make ``write``. A write that ends the task frees its slot and prunes old records."""
        try:
            flushed = self._write_record(write.task_id, write.fields)
            if write.slot is not None and (write.ends or flushed is _Outcome.LOST):
                self._slots.release(write.slot, write.task_id)
            if write.ends:
                self._prune()
        except (PyMongoError, DocumentConflictError) as exc:
            return _Outcome.FAILED, str(exc)
        return flushed, None

    def _write_record(self, task_id: str, fields: dict[str, Any]) -> _Outcome:
        now = time.time()

        def update(stored: TaskRecord) -> TaskRecord:
            if stored.owner != self._owner or stored.status.is_terminal:
                raise _UnwritableError(task_id)
            return stored.model_copy(update=fields | {"heartbeat_at": now})

        try:
            self._records.mutate(task_id, update)
        except KeyError, _UnwritableError:
            return _Outcome.LOST
        except InvalidDocument:
            if fields["result"] is None:
                raise
            return self._write_record(task_id, _result_dropped(fields))
        return _Outcome.WRITTEN

    def _prune(self) -> None:
        """Delete the ended records beyond the newest ``max_completed``."""
        beyond = (
            self._raw.find({"status": {"$in": _ENDED}}, {"id": 1, "_id": 0})
            .sort("completed_at", DESCENDING)
            .skip(self._max_completed)
        )
        for doc in builtins.list(beyond):
            self._records.delete(doc["id"], only_if={"status": {"$in": _ENDED}})


def _watcher_ended(watcher: asyncio.Task) -> None:
    if not watcher.cancelled() and watcher.exception() is not None:
        logger.error(
            "task owner watch stopped, the next submit restarts it",
            error=str(watcher.exception()),
        )
