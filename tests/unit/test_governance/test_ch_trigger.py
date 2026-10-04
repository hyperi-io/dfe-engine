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

import itertools
import threading
import time
from types import SimpleNamespace

import clickhouse_connect
from prometheus_client.parser import text_string_to_metric_families
from scalo.metrics import create_metrics

from dfe_engine.governance.ch import (
    ReconcileMetrics,
    ReconcileResult,
    ReconcileTrigger,
    note_ch_rbac_reconciled,
)
from dfe_engine.governance.ch.trigger import RECONCILES, RETRIES

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


def _wait_for(check, timeout: float = _WAIT) -> bool:
    """Poll ``check`` until it holds or ``timeout`` passes; returns whether it held."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.02)
    return check()


def test_a_failed_reconcile_is_counted_and_run_again_without_another_change() -> None:
    """An org created while ClickHouse is down is provisioned once it is back."""
    manager = _manager()
    recorder = _Recorder()
    trigger = ReconcileTrigger(
        recorder,
        metrics=ReconcileMetrics(manager),
        settle_seconds=_SETTLE,
        retry_initial_seconds=0.1,
    )
    recorder.raise_next = ConnectionRefusedError("clickhouse is down")

    trigger.request()

    assert trigger.wait_idle(_WAIT)
    assert recorder.calls == 2
    assert _outcomes(manager) == {"failed": 1.0, "ok": 1.0}


def test_retries_back_off_and_stop_growing_at_the_cap() -> None:
    calls: list[float] = []

    def down() -> ReconcileResult:
        calls.append(time.monotonic())
        raise ConnectionRefusedError("clickhouse is down")

    trigger = ReconcileTrigger(
        down, settle_seconds=0.0, retry_initial_seconds=0.1, retry_max_seconds=0.4
    )

    trigger.request()
    assert _wait_for(lambda: len(calls) >= 6)
    assert trigger.close(_WAIT)

    # Nominal 0.1, 0.2, then 0.4 each, each jittered into its upper half; uncapped
    # the fifth gap would be 1.6.
    gaps = [later - earlier for earlier, later in itertools.pairwise(calls)]
    assert gaps[0] >= 0.05
    assert gaps[1] >= 0.1
    assert all(gap >= 0.2 for gap in gaps[2:5])
    assert max(gaps[2:5]) < 0.8


def test_each_retry_wait_is_jittered_within_the_upper_half_of_its_back_off() -> None:
    trigger = ReconcileTrigger(ReconcileResult, retry_initial_seconds=4.0, retry_max_seconds=300.0)

    waits = {failures: [trigger._backoff(failures) for _ in range(200)] for failures in (1, 3, 9)}

    assert all(2.0 <= w <= 4.0 for w in waits[1])
    assert all(8.0 <= w <= 16.0 for w in waits[3])
    assert all(150.0 <= w <= 300.0 for w in waits[9])
    assert len({round(w, 6) for w in waits[9]}) > 1, "the waits at the cap are all one value"


def _retries(manager) -> dict[str, float]:
    counts: dict[str, float] = {}
    for family in text_string_to_metric_families(manager.metrics_text):
        for sample in family.samples:
            if sample.name == RETRIES:
                counts[sample.labels["reconcile"]] = sample.value
    return counts


def test_a_startup_run_that_gets_through_starts_no_background_run() -> None:
    manager = _manager()
    recorder = _Recorder()
    trigger = ReconcileTrigger(recorder, metrics=ReconcileMetrics(manager))

    assert trigger.run_now() is True

    assert trigger.wait_idle(0.0)
    assert recorder.calls == 1
    assert _outcomes(manager) == {"ok": 1.0}
    assert _retries(manager) == {}


def test_a_startup_run_that_fails_is_retried_until_it_gets_through() -> None:
    """ClickHouse down at the engine's boot: the users arrive once it is back, no restart."""
    manager = _manager()
    recorder = _Recorder()
    failures = iter([ConnectionRefusedError("clickhouse is down")] * 3)

    def down_three_times() -> ReconcileResult:
        recorder.raise_next = next(failures, None)
        return recorder()

    trigger = ReconcileTrigger(
        down_three_times,
        metrics=ReconcileMetrics(manager),
        reconcile="service_roles",
        retry_initial_seconds=0.05,
    )

    assert trigger.run_now() is False
    assert trigger.wait_idle(_WAIT)

    assert recorder.calls == 4
    assert _outcomes(manager) == {"failed": 3.0, "ok": 1.0}
    assert _retries(manager) == {"service_roles": 3.0}


def test_a_reconcile_that_got_through_elsewhere_stands_a_retry_down() -> None:
    """The governance endpoint's run covers the identities the retry was waiting to make."""
    recorder = _Recorder()
    recorder.raise_next = ConnectionRefusedError("clickhouse is down")
    trigger = ReconcileTrigger(recorder, settle_seconds=0.0, retry_initial_seconds=_WAIT * 6)

    assert trigger.run_now() is False
    started = time.monotonic()
    trigger.succeeded()

    assert trigger.wait_idle(_WAIT / 2)
    assert time.monotonic() - started < _WAIT / 2
    assert recorder.calls == 1


def test_a_change_waiting_behind_a_retry_still_runs_after_one_got_through_elsewhere() -> None:
    recorder = _Recorder()
    recorder.raise_next = ConnectionRefusedError("clickhouse is down")
    trigger = ReconcileTrigger(recorder, settle_seconds=0.0, retry_initial_seconds=0.3)

    assert trigger.run_now() is False
    trigger.request()
    trigger.succeeded()

    assert trigger.wait_idle(_WAIT)
    assert recorder.calls == 2


def test_note_reconciled_stands_down_whichever_trigger_the_app_carries() -> None:
    recorder = _Recorder()
    recorder.raise_next = ConnectionRefusedError("clickhouse is down")
    trigger = ReconcileTrigger(recorder, reconcile="service_roles", retry_initial_seconds=_WAIT * 6)
    state = SimpleNamespace(ch_service_role_reconcile=trigger)

    assert trigger.run_now() is False
    note_ch_rbac_reconciled(state)

    assert trigger.wait_idle(_WAIT / 2)
    assert recorder.calls == 1


def test_a_reconcile_with_failed_statements_is_counted_partial_and_not_retried() -> None:
    # Its failed statements fail the same way on the next run.
    manager = _manager()
    recorder = _Recorder(errors=["GRANT ...: denied"])
    trigger = ReconcileTrigger(
        recorder,
        metrics=ReconcileMetrics(manager),
        settle_seconds=_SETTLE,
        retry_initial_seconds=0.05,
    )

    trigger.request()
    assert trigger.wait_idle(_WAIT)
    assert _outcomes(manager) == {"partial": 1.0}
    assert recorder.calls == 1


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
    trigger = ReconcileTrigger(
        reconcile,
        metrics=ReconcileMetrics(manager),
        settle_seconds=0.0,
        retry_initial_seconds=_WAIT * 6,
    )

    trigger.request()
    assert _wait_for(lambda: _outcomes(manager) == {"failed": 1.0})
    assert trigger.close(_WAIT)
    assert _outcomes(manager) == {"failed": 1.0}


def test_close_ends_the_wait_before_a_retry() -> None:
    recorder = _Recorder()
    recorder.raise_next = ConnectionRefusedError("clickhouse is down")
    trigger = ReconcileTrigger(recorder, settle_seconds=0.0, retry_initial_seconds=_WAIT * 6)

    trigger.request()
    assert _wait_for(lambda: recorder.calls == 1 and recorder.running == 0)
    started = time.monotonic()

    assert trigger.close(_WAIT)
    assert time.monotonic() - started < _WAIT / 2
    assert recorder.calls == 1


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
