#  Project:      dfe-engine
#  File:         tests/unit/test_clickhouse/test_readiness_ping.py
#  Purpose:      CH manager readiness ping + fail-closed /readyz wiring
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The CH manager's readiness probe (:meth:`ClickHouseManager.ping`) and its
fail-closed contribution to readiness.

``ping`` is the bounded, resilience-BYPASSING reachability check that gates
``/readyz``: a kubelet poll must fail fast, must never sit inside the
reconnect/auto-wake budget, and must never wake a paused CH Cloud. The only
double is the clickhouse-connect driver client - the external boundary - so no
real ClickHouse is touched and nothing internal is mocked. The end-to-end
fail-closed proof runs scalo's REAL :class:`~scalo.health.HealthManager`.
"""

from __future__ import annotations

from scalo.health import HealthManager

from dfe_engine.clickhouse.clickhouse_manager import ClickHouseManager


class _FakeDriverClient:
    """Stands in for the clickhouse-connect driver client (external boundary).

    ``ping()`` returns a scripted bool or raises a scripted error, so the
    wrapper's fast-fail + never-raise contract is exercised without a real CH.
    """

    def __init__(self, *, result: bool = True, error: Exception | None = None) -> None:
        self._result = result
        self._error = error
        self.pings = 0

    def ping(self) -> bool:
        self.pings += 1
        if self._error is not None:
            raise self._error
        return self._result


class _PingManager(ClickHouseManager):
    """Manager whose live client is a driver double (or a build that raises).

    Overrides ONLY the external driver-client boundary; the ping wrapper under
    test runs for real. ``client=None`` models a client that cannot be built (a
    connect failure) - ``_live_client`` raises, which the wrapper must swallow.
    """

    def __init__(self, client: _FakeDriverClient | None) -> None:
        super().__init__({})
        self._fake = client

    def _live_client(self):  # type: ignore[override]
        if self._fake is None:
            raise ConnectionError("connection refused")
        return self._fake


class TestPing:
    def test_reachable_returns_true(self):
        client = _FakeDriverClient(result=True)
        mgr = _PingManager(client)

        assert mgr.ping() is True
        assert client.pings == 1  # a SINGLE check - no retry loop

    def test_unreachable_returns_false(self):
        mgr = _PingManager(_FakeDriverClient(result=False))

        assert mgr.ping() is False

    def test_ping_error_never_propagates(self):
        # A driver-level error reads as not-ready, never an exception surfacing
        # into the readiness handler.
        mgr = _PingManager(_FakeDriverClient(error=RuntimeError("boom")))

        assert mgr.ping() is False

    def test_client_build_failure_returns_false(self):
        # CH unreachable at connect time -> _live_client raises -> not ready.
        # Fast fail: the wrapper deliberately does NOT run through run_resilient.
        mgr = _PingManager(None)

        assert mgr.ping() is False


class TestReadinessFailClosed:
    """End-to-end via scalo's real HealthManager: /readyz is fail-closed on CH."""

    def test_ready_only_when_ch_reachable(self):
        health = HealthManager()
        mgr = _PingManager(_FakeDriverClient(result=True))
        health.register_ready_check("clickhouse", mgr.ping)

        health.set_ready()  # the latch is set AND the check passes

        assert health.is_ready() is True

    def test_not_ready_when_ch_down_even_after_set_ready(self):
        health = HealthManager()
        mgr = _PingManager(_FakeDriverClient(result=False))
        health.register_ready_check("clickhouse", mgr.ping)

        health.set_ready()  # the latch is set...

        assert health.is_ready() is False  # ...but CH down keeps /readyz NotReady
