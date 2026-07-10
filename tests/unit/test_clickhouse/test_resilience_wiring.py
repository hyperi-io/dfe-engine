#  Project:      dfe-engine
#  File:         tests/unit/test_clickhouse/test_resilience_wiring.py
#  Purpose:      CH manager <-> scalo ReconnectingResilience wiring
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The CH manager's binding of scalo's ReconnectingResilience.

Exercises the WIRING (not scalo's engine, which scalo tests): the manager builds
a :class:`~scalo.resilience.ReconnectingResilience` with the CH classifiers, the
reconnect, and the CH Cloud auto-wake hook, sourced from the ``clickhouse``
settings. Faults are injected as REAL exceptions (driver-style ``Code: NNN``
strings and Python ``ConnectionError``) - no ClickHouse and no mocks of internal
code; the clock is faked so the back-off never really sleeps or races a wall
clock. The ONLY double is the external CH Cloud lifecycle (a billable network
call), the sanctioned external-paid-service exception - it records ``start()``
and never touches the network.
"""

from __future__ import annotations

import pytest
from scalo.resilience import OutageState, ServiceUnavailable

from dfe_engine.clickhouse.clickhouse_manager import ClickHouseManager
from dfe_engine.clickhouse.cloud import CloudServiceStatus
from dfe_engine.settings import ClickHouseResilienceSettings, DFESettings


class _Clock:
    """Injectable clock - sleep advances a monotonic counter, never a wall clock."""

    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


class _FlakyOp:
    """A REAL op that raises a scripted error sequence, then returns a value.

    Not a mock - a plain recording callable, so the assertions read its real call
    count. ``errors`` are raised one per call (front to back); once drained the op
    returns ``result``.
    """

    def __init__(self, errors: list[Exception], result: object = "ok") -> None:
        self._errors = list(errors)
        self._result = result
        self.calls = 0

    def __call__(self) -> object:
        self.calls += 1
        if self._errors:
            raise self._errors.pop(0)
        return self._result


class _RecordingManager(ClickHouseManager):
    """Manager that COUNTS reconnects (observes real behaviour; calls through)."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.reconnects = 0

    def _reconnect(self) -> None:
        self.reconnects += 1
        super()._reconnect()  # real teardown - a safe no-op with no live client


class _FakeCloudService:
    """External CH Cloud lifecycle double (sanctioned external-service exception).

    Records the billable ``start()`` and returns a REAL CloudServiceStatus - it
    never opens a socket, so no paid instance is ever touched.
    """

    def __init__(self, state: str = "stopped") -> None:
        self._state = state
        self.start_calls = 0
        self.status_calls = 0

    def status(self) -> CloudServiceStatus:
        self.status_calls += 1
        return CloudServiceStatus(id="svc-test", name="dfe", state=self._state)

    def start(self) -> CloudServiceStatus:
        self.start_calls += 1
        self._state = "starting"
        return CloudServiceStatus(id="svc-test", name="dfe", state=self._state)


def _settings(
    *,
    autowake: bool = False,
    enabled: bool = True,
    budget: float = 1.0,
    waking_budget: float = 1000.0,
    wait_initial: float = 1.0,
    wait_max: float = 1.0,
    wait_multiplier: float = 1.0,
) -> DFESettings:
    settings = DFESettings()
    settings.clickhouse.resilience = ClickHouseResilienceSettings(
        enabled=enabled,
        wait_initial=wait_initial,
        wait_max=wait_max,
        wait_multiplier=wait_multiplier,
        budget_seconds=budget,
        waking_budget_seconds=waking_budget,
    )
    settings.clickhouse.cloud.autowake = autowake
    return settings


def _manager(settings: DFESettings, *, clock: _Clock, cloud=None) -> _RecordingManager:
    return _RecordingManager(
        {},
        settings=settings,
        sleep=clock.sleep,
        now=clock.now,
        cloud_service=cloud,
    )


def _conn_err() -> ConnectionError:
    return ConnectionError("connection refused")


def _rate_limited() -> Exception:
    return Exception("Code: 202. DB::Exception: Too many simultaneous queries")


def _syntax_err() -> Exception:
    return Exception("Code: 62. DB::Exception: Syntax error")


class TestTransientRecovery:
    def test_connection_outage_reconnects_then_recovers(self):
        clock = _Clock()
        mgr = _manager(_settings(), clock=clock)
        op = _FlakyOp([_conn_err()], result="rows")

        result = mgr.run_resilient(op)

        assert result == "rows"
        assert op.calls == 2  # first raised, retry succeeded
        assert mgr.reconnects == 1  # a CONNECTION outage rebuilds the client
        assert mgr._get_resilience().state is OutageState.HEALTHY

    def test_rate_limit_backs_off_without_reconnect(self):
        clock = _Clock()
        mgr = _manager(_settings(), clock=clock)
        op = _FlakyOp([_rate_limited()], result="rows")

        result = mgr.run_resilient(op)

        assert result == "rows"
        assert op.calls == 2
        assert mgr.reconnects == 0  # the connection is fine - do NOT reconnect
        assert mgr._get_resilience().state is OutageState.HEALTHY


class TestNonTransient:
    def test_query_error_surfaces_immediately(self):
        clock = _Clock()
        mgr = _manager(_settings(), clock=clock)
        op = _FlakyOp([_syntax_err()], result="unreached")

        with pytest.raises(Exception) as excinfo:
            mgr.run_resilient(op)

        assert "Code: 62" in str(excinfo.value)
        assert not isinstance(excinfo.value, ServiceUnavailable)
        assert op.calls == 1  # never retried
        assert mgr.reconnects == 0


class TestBudgetExhaustion:
    def test_persistent_outage_raises_service_unavailable(self):
        clock = _Clock()
        # Short budget, no auto-wake -> a persistent connection outage dies as 503.
        mgr = _manager(_settings(budget=1.0), clock=clock)
        op = _FlakyOp([_conn_err() for _ in range(50)], result="unreached")

        with pytest.raises(ServiceUnavailable) as excinfo:
            mgr.run_resilient(op)

        assert excinfo.value.waking is False
        assert mgr._get_resilience().state is OutageState.DEAD
        assert mgr.reconnects >= 1  # tried to reconnect before giving up


class TestAutowakeHook:
    def test_autowake_fires_and_extends_budget_to_recover(self):
        clock = _Clock()
        cloud = _FakeCloudService(state="stopped")
        # Short base budget (1s) but a generous waking budget: without the wake the
        # 5-failure outage would exhaust the base budget; WITH it, it recovers.
        mgr = _manager(
            _settings(autowake=True, budget=1.0, waking_budget=1000.0),
            clock=clock,
            cloud=cloud,
        )
        op = _FlakyOp([_conn_err() for _ in range(5)], result="rows")

        result = mgr.run_resilient(op)

        assert result == "rows"
        assert cloud.start_calls == 1  # the billable resume fired exactly once
        assert mgr._get_resilience().state is OutageState.HEALTHY

    def test_without_autowake_same_outage_dies(self):
        # Companion to the above: identical outage + base budget, hook NOT wired ->
        # the short budget is exhausted, proving the wake is what saved it.
        clock = _Clock()
        cloud = _FakeCloudService(state="stopped")
        mgr = _manager(
            _settings(autowake=False, budget=1.0, waking_budget=1000.0),
            clock=clock,
            cloud=cloud,
        )
        op = _FlakyOp([_conn_err() for _ in range(5)], result="rows")

        with pytest.raises(ServiceUnavailable):
            mgr.run_resilient(op)

        # autowake off -> on_connect_failure is None -> the Cloud seam is untouched.
        assert cloud.start_calls == 0
        assert cloud.status_calls == 0


class TestDisabled:
    def test_disabled_resilience_runs_once_and_raises(self):
        clock = _Clock()
        mgr = _manager(_settings(enabled=False), clock=clock)
        op = _FlakyOp([_conn_err()], result="unreached")

        with pytest.raises(ConnectionError):
            mgr.run_resilient(op)

        assert op.calls == 1  # no retry when disabled
        assert mgr.reconnects == 0

    def test_disabled_resilience_passes_through_success(self):
        clock = _Clock()
        mgr = _manager(_settings(enabled=False), clock=clock)
        op = _FlakyOp([], result="rows")

        assert mgr.run_resilient(op) == "rows"
        assert op.calls == 1
