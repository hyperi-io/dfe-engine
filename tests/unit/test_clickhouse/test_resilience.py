#  Project:      dfe-engine
#  File:         tests/unit/test_clickhouse/test_resilience.py
#  Purpose:      Unit tests for CH connection resilience (Phase 0)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""ChResilience back-off + outage-state tests.

Deterministic: an injected clock makes ``sleep`` advance a fake monotonic time,
so the budget is exercised without a real wall clock (no timing flake) and the
suite never actually sleeps.
"""

from __future__ import annotations

import pytest

from dfe_engine.clickhouse.resilience import (
    ChResilience,
    ChUnavailable,
    OutageState,
    ResilienceConfig,
    is_connection_error,
)


class FakeClock:
    """A monotonic clock whose ``sleep`` advances the clock (no real waiting)."""

    def __init__(self) -> None:
        self.t = 0.0
        self.slept: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds


def _resilience(clock: FakeClock, *, on_wake=None, reconnect=None, **cfg):
    reconnects: list[int] = []

    def _default_reconnect() -> None:
        reconnects.append(1)

    r = ChResilience(
        ResilienceConfig(**cfg),
        reconnect=reconnect or _default_reconnect,
        on_connect_failure=on_wake,
        sleep=clock.sleep,
        now=clock.now,
    )
    r.reconnects = reconnects  # type: ignore[attr-defined]
    return r


def _conn_error(msg: str = "Connection refused") -> Exception:
    return ConnectionError(msg)


class TestIsConnectionError:
    def test_connection_keywords_are_retryable(self):
        assert is_connection_error(ConnectionError("Connection refused"))
        assert is_connection_error(Exception("read timed out"))
        assert is_connection_error(Exception("Max retries exceeded"))

    def test_query_errors_are_not_connection_errors(self):
        # A CH syntax / memory / access error must NOT be treated as an outage.
        assert not is_connection_error(Exception("Code: 62. Syntax error"))
        assert not is_connection_error(Exception("Code: 241. Memory limit exceeded"))
        assert not is_connection_error(Exception("Code: 193. Authentication failed"))

    def test_ch_connection_codes_are_retryable(self):
        assert is_connection_error(Exception("Code: 209. Connection timeout"))
        assert is_connection_error(Exception("Code: 210. Connection lost"))


class TestRun:
    def test_success_first_try_is_healthy_no_reconnect(self):
        clock = FakeClock()
        r = _resilience(clock)
        assert r.run(lambda: "ok") == "ok"
        assert r.state == OutageState.HEALTHY
        assert r.reconnects == []  # type: ignore[attr-defined]

    def test_non_connection_error_raises_immediately(self):
        clock = FakeClock()
        r = _resilience(clock)
        with pytest.raises(Exception, match="Syntax"):
            r.run(lambda: (_ for _ in ()).throw(Exception("Code: 62. Syntax error")))
        assert r.reconnects == []  # type: ignore[attr-defined] - never entered the outage loop
        assert not clock.slept

    def test_recovers_after_transient_outage(self):
        clock = FakeClock()
        r = _resilience(clock, wait_initial=0.5, wait_max=10.0, budget_seconds=60.0)
        calls = {"n": 0}

        def op() -> str:
            calls["n"] += 1
            if calls["n"] <= 2:
                raise _conn_error()
            return "recovered"

        assert r.run(op) == "recovered"
        assert r.state == OutageState.HEALTHY
        assert len(r.reconnects) == 2  # type: ignore[attr-defined] - rebuilt before each retry
        assert clock.slept == [0.5, 1.0]  # exponential back-off

    def test_dead_after_budget_exhausted(self):
        clock = FakeClock()
        r = _resilience(clock, wait_initial=1.0, wait_max=4.0, budget_seconds=10.0)

        with pytest.raises(ChUnavailable) as exc:
            r.run(lambda: (_ for _ in ()).throw(_conn_error()))
        assert r.state == OutageState.DEAD
        assert not exc.value.waking

    def test_auto_wake_hook_flips_to_waking_and_extends_budget(self):
        clock = FakeClock()
        wake_calls = {"n": 0}

        def on_wake() -> bool:
            wake_calls["n"] += 1
            return True  # a wake was initiated -> WAKING, extended budget

        # Normal budget 5s would give up fast; waking budget 300s keeps trying.
        r = _resilience(
            clock,
            wait_initial=1.0,
            wait_max=10.0,
            budget_seconds=5.0,
            waking_budget_seconds=300.0,
            on_wake=on_wake,
        )
        calls = {"n": 0}

        def op() -> str:
            calls["n"] += 1
            # Fails for ~30s of fake time (would exceed the 5s normal budget),
            # succeeds only because the WAKING budget is 300s.
            if clock.now() < 30.0:
                raise _conn_error("clickhouse warming up")
            return "awake"

        assert r.run(op) == "awake"
        assert wake_calls["n"] == 1  # hook fired exactly once, at outage start
        assert r.state == OutageState.HEALTHY

    def test_disabled_passes_through(self):
        clock = FakeClock()
        r = _resilience(clock, enabled=False)
        with pytest.raises(ConnectionError):
            r.run(lambda: (_ for _ in ()).throw(_conn_error()))
        assert r.reconnects == []  # type: ignore[attr-defined] - resilience off, no retry
