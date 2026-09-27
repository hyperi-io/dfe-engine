#  Project:      dfe-engine
#  File:         tests/unit/test_governance/test_ch_trigger.py
#  Purpose:      The background CH RBAC reconcile runs once per burst, one at a time
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The trigger an org or group change fires, driven with a real call recorder.

The reconcile stands in as a recorder of its own calls, so each test counts what
ran, and a real prometheus manager counts how each run ended.
"""

import threading
import time

import clickhouse_connect
from prometheus_client.parser import text_string_to_metric_families
from scalo.metrics import create_metrics

from dfe_engine.governance.ch import ReconcileMetrics, ReconcileResult, ReconcileTrigger
from dfe_engine.governance.ch.trigger import RECONCILES

_SETTLE = 0.2
_WAIT = 10.0


class _Recorder:
    """A reconcile that records each call; it can hold a call open or fail it."""

    def __init__(self, *, errors: list[str] | None = None) -> None:
        self.calls = 0
        self.running = 0
        self.most_at_once = 0
        self.started = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.raise_next: Exception | None = None
        self._errors = errors or []
        self._lock = threading.Lock()

    def __call__(self) -> ReconcileResult:
        with self._lock:
            self.calls += 1
            self.running += 1
            self.most_at_once = max(self.most_at_once, self.running)
        self.started.set()
        try:
            self.release.wait(_WAIT)
            if self.raise_next is not None:
                exc, self.raise_next = self.raise_next, None
                raise exc
            return ReconcileResult(errors=list(self._errors))
        finally:
            with self._lock:
                self.running -= 1


def _outcomes(manager) -> dict[str, float]:
    counts: dict[str, float] = {}
    for family in text_string_to_metric_families(manager.metrics_text):
        for sample in family.samples:
            if sample.name == RECONCILES:
                counts[sample.labels["outcome"]] = sample.value
    return counts


def _manager():
    return create_metrics("test", backend="prometheus", enable_auto_update=False)


def test_a_burst_of_changes_is_one_reconcile() -> None:
    recorder = _Recorder()
    trigger = ReconcileTrigger(recorder, settle_seconds=_SETTLE)

    for _ in range(10):
        trigger.request()
    assert trigger.wait_idle(_WAIT)
    assert recorder.calls == 1

    trigger.request()
    assert trigger.wait_idle(_WAIT)
    assert recorder.calls == 2


def test_request_returns_before_the_reconcile_runs() -> None:
    """The write that asked must not wait for ClickHouse."""
    recorder = _Recorder()
    recorder.release.clear()
    trigger = ReconcileTrigger(recorder, settle_seconds=0.0)

    started = time.monotonic()
    trigger.request()
    assert recorder.started.wait(_WAIT)
    trigger.request()
    elapsed = time.monotonic() - started

    assert elapsed < 1.0
    recorder.release.set()
    assert trigger.wait_idle(_WAIT)


def test_a_change_during_a_run_gets_exactly_one_more_run() -> None:
    """The run in flight may have read the stores before the change, so one more follows."""
    recorder = _Recorder()
    recorder.release.clear()
    trigger = ReconcileTrigger(recorder, settle_seconds=_SETTLE)

    trigger.request()
    assert recorder.started.wait(_WAIT)
    for _ in range(5):
        trigger.request()
    recorder.release.set()

    assert trigger.wait_idle(_WAIT)
    assert recorder.calls == 2
    assert recorder.most_at_once == 1


def test_a_failed_reconcile_is_counted_and_the_next_change_runs_it_again() -> None:
    manager = _manager()
    recorder = _Recorder()
    trigger = ReconcileTrigger(recorder, metrics=ReconcileMetrics(manager), settle_seconds=_SETTLE)
    recorder.raise_next = ConnectionRefusedError("clickhouse is down")

    trigger.request()
    assert trigger.wait_idle(_WAIT)
    assert _outcomes(manager) == {"failed": 1.0}

    trigger.request()
    assert trigger.wait_idle(_WAIT)
    assert recorder.calls == 2
    assert _outcomes(manager) == {"failed": 1.0, "ok": 1.0}


def test_a_reconcile_with_failed_statements_is_counted_partial() -> None:
    manager = _manager()
    trigger = ReconcileTrigger(
        _Recorder(errors=["GRANT ...: denied"]),
        metrics=ReconcileMetrics(manager),
        settle_seconds=_SETTLE,
    )

    trigger.request()
    assert trigger.wait_idle(_WAIT)
    assert _outcomes(manager) == {"partial": 1.0}


def test_a_reconcile_against_an_unreachable_clickhouse_is_counted_failed() -> None:
    """The failure a real deployment sees: the admin client cannot connect."""
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    def reconcile() -> ReconcileResult:
        client = clickhouse_connect.get_client(
            host="127.0.0.1", port=port, connect_timeout=1, send_receive_timeout=1
        )
        client.command("SELECT 1")
        return ReconcileResult()

    manager = _manager()
    trigger = ReconcileTrigger(reconcile, metrics=ReconcileMetrics(manager), settle_seconds=0.0)

    trigger.request()
    assert trigger.wait_idle(_WAIT)
    assert _outcomes(manager) == {"failed": 1.0}


def test_close_drops_a_settling_run_and_refuses_new_ones() -> None:
    recorder = _Recorder()
    trigger = ReconcileTrigger(recorder, settle_seconds=_WAIT)

    trigger.request()
    started = time.monotonic()
    assert trigger.close(_WAIT)
    assert time.monotonic() - started < _WAIT / 2

    trigger.request()
    assert trigger.wait_idle(_WAIT)
    assert recorder.calls == 0


def test_close_waits_for_the_run_in_flight() -> None:
    recorder = _Recorder()
    recorder.release.clear()
    trigger = ReconcileTrigger(recorder, settle_seconds=0.0)

    trigger.request()
    assert recorder.started.wait(_WAIT)
    assert trigger.close(0.1) is False
    recorder.release.set()
    assert trigger.close(_WAIT) is True
    assert recorder.calls == 1
