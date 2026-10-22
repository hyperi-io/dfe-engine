#  Project:      dfe-engine
#  File:         tests/integration/test_document_tasks_off_loop.py
#  Purpose:      A route that reaches the task store waits on it off the event loop
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A stalled task store call holds up only the request waiting on it.

Each case starts the engine with its tasks in the harness's document store
(``document_engine``), stalls the store call one route makes through the task manager,
and asks ``/livez`` for an answer meanwhile. A route whose call ran on the event loop
holds ``/livez`` up with it, as it would every request, the probes and the heartbeat of
every task this replica owns.
"""

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from dfe_engine.api.document_tasks import TaskRecord
from dfe_engine.api.task_manager import TaskStatus
from tests.support.stalled_call import Setup, Stall, request_while_stalled

pytestmark = pytest.mark.integration

STREAM_KIND = "synthetic-data:stream"
# Low rate at an address nothing listens on, so the stream stays running until stopped.
LONG_STREAM = {
    "schema": "meta/syslog",
    "duration_s": 60,
    "rate_eps": 0.5,
    "receiver_url": "http://127.0.0.1:9/ingest",
}
SAMPLE = {"mode": "recent", "table": "`db`.`events`"}
SOURCE = "offloop"
PROVIDER = "files"

type Prepare = Callable[[SimpleNamespace, Path], str]
"""Set up what a route needs before its call stalls, and return the task id it names."""


def _store_call(name: str, *, on: str = "_records", skip: int = 0) -> Setup:
    """Stall the task manager's store call *name*.

    Args:
        name: The method to stall.
        on: The manager's attribute holding it, or ``""`` for the manager itself.
        skip: Calls to let through before the one that stalls.
    """

    def setup(app: Any, stall: Stall, monkeypatch: pytest.MonkeyPatch) -> None:
        manager = app.state.task_manager
        target = getattr(manager, on) if on else manager
        stall.skip = skip
        monkeypatch.setattr(target, name, stall.call(getattr(target, name)))

    return setup


_READ = _store_call("get")
_LIST = _store_call("_find", on="")
_WRITE = _store_call("insert")
_ASK = _store_call("mutate")


def _unknown(stack: SimpleNamespace, tmp_path: Path) -> str:
    return str(uuid.uuid4())


def _remote(kind: str) -> Prepare:
    """A running task of *kind* another replica owns, its heartbeat fresh."""

    def prepare(stack: SimpleNamespace, tmp_path: Path) -> str:
        task_id = str(uuid.uuid4())
        record = TaskRecord(
            id=task_id,
            kind=kind,
            owner="replica-elsewhere",
            status=TaskStatus.RUNNING,
            created_at=datetime.now(UTC).isoformat(),
            heartbeat_at=time.time(),
        )
        assert stack.app.state.task_manager._records.insert(record)
        return task_id

    return prepare


def _source(stack: SimpleNamespace, tmp_path: Path) -> str:
    created = stack.client.post(
        "/api/v1/sources",
        headers=stack.headers,
        json={
            "display_name": SOURCE,
            "enabled": True,
            "header": {"type": "timeseries", "version": "1.0.0"},
            "match": {"field": "tags.collector.type", "value": SOURCE},
            "schema": {"meta_schema": "meta/syslog"},
            "source": SOURCE,
        },
    )
    assert created.status_code in (200, 201), created.text
    return ""


def _provider(stack: SimpleNamespace, tmp_path: Path) -> str:
    rules = tmp_path / "sigma-import"
    rules.mkdir()
    registered = stack.client.post(
        "/api/v1/sigma/providers",
        headers=stack.headers,
        json={"name": PROVIDER, "kind": "local_files", "options": {"directory": str(rules)}},
    )
    assert registered.status_code in (200, 201), registered.text
    return ""


@dataclass(frozen=True, slots=True)
class _Case:
    """What stalls, the request that waits on it, and what it needs first."""

    stall: Setup
    method: str
    path: str
    body: dict | None = None
    prepare: Prepare = _unknown
    sigma: bool = False


_CASES: dict[str, _Case] = {
    "list tasks": _Case(_LIST, "GET", "/api/v1/tasks"),
    "read a task": _Case(_READ, "GET", "/api/v1/tasks/{task_id}"),
    "stream a task": _Case(_READ, "GET", "/api/v1/tasks/{task_id}/stream"),
    "a cancel's check": _Case(
        _READ, "POST", "/api/v1/tasks/{task_id}/cancel", prepare=_remote("test:remote")
    ),
    "a cancel's ask of the owner": _Case(
        _ASK, "POST", "/api/v1/tasks/{task_id}/cancel", prepare=_remote("test:remote")
    ),
    "a cancel's read back": _Case(
        _store_call("get", skip=2),
        "POST",
        "/api/v1/tasks/{task_id}/cancel",
        prepare=_remote("test:remote"),
    ),
    "submit a sample": _Case(_WRITE, "POST", "/api/v1/sample", SAMPLE | {"wait": 0}),
    "wait for a sample": _Case(_READ, "POST", "/api/v1/sample", SAMPLE | {"wait": 5}),
    "poll a sample": _Case(_READ, "GET", "/api/v1/samples/{task_id}"),
    "list samples": _Case(_LIST, "GET", "/api/v1/samples"),
    "submit a sigma sync": _Case(
        _WRITE, "POST", f"/api/v1/sigma/providers/{PROVIDER}/sync?wait=0", None, _provider, True
    ),
    "wait for a sigma sync": _Case(
        _READ, "POST", f"/api/v1/sigma/providers/{PROVIDER}/sync?wait=5", None, _provider, True
    ),
    "poll a sigma sync": _Case(_READ, "GET", "/api/v1/sigma/syncs/{task_id}", sigma=True),
    "submit a propagation": _Case(_WRITE, "POST", "/api/v1/sigma/propagate?wait=0", sigma=True),
    "wait for a propagation": _Case(_READ, "POST", "/api/v1/sigma/propagate?wait=5", sigma=True),
    "poll a propagation": _Case(_READ, "GET", "/api/v1/sigma/propagations/{task_id}", sigma=True),
    "start a stream": _Case(_WRITE, "POST", "/api/v1/synthetic-data/stream", LONG_STREAM),
    "wait for a stream": _Case(
        _READ, "POST", "/api/v1/synthetic-data/stream", LONG_STREAM | {"wait": 2}
    ),
    "list streams": _Case(_LIST, "GET", "/api/v1/synthetic-data/streams"),
    "poll a stream": _Case(_READ, "GET", "/api/v1/synthetic-data/streams/{task_id}"),
    "a stream cancel's check": _Case(
        _READ, "DELETE", "/api/v1/synthetic-data/streams/{task_id}", prepare=_remote(STREAM_KIND)
    ),
    "a stream cancel's ask of the owner": _Case(
        _ASK, "DELETE", "/api/v1/synthetic-data/streams/{task_id}", prepare=_remote(STREAM_KIND)
    ),
    "build a pipeline": _Case(_WRITE, "POST", "/api/v1/pipeline/build", {"build_core": False}),
    "deploy sources in bulk": _Case(
        _WRITE,
        "POST",
        "/api/v1/sources/bulk",
        {"action": "deploy", "sources": [SOURCE], "dry_run": True},
        _source,
    ),
}


@pytest.mark.parametrize("case", list(_CASES))
def test_a_task_store_call_holds_up_only_its_own_request(case, document_engine, tmp_path):
    spec = _CASES[case]
    document_engine.settings.sigma.enabled = spec.sigma
    with document_engine.start() as stack:
        path = spec.path.format(task_id=spec.prepare(stack, tmp_path))
        stall = Stall()
        with pytest.MonkeyPatch.context() as monkeypatch:
            spec.stall(stack.app, stall, monkeypatch)
            done = request_while_stalled(
                stack.client, stall, case, spec.method, path, json=spec.body, headers=stack.headers
            )

    assert done.status_code < 500, done.text
