#  Project:      dfe-engine
#  File:         tests/hunt_runner/test_connect.py
#  Purpose:      The runner waits out a refused login or an outage instead of exiting
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""``connect_when_available`` and ``TickGuard``, with real prometheus counters.

The attempts stand in for a clickhouse-connect client raising what the driver
raises: a ``Code: NNN`` message for a server refusal, a ``ConnectionRefusedError``
for an outage. The back-off is recorded rather than slept.
"""

import pytest
from prometheus_client.parser import text_string_to_metric_families
from scalo.metrics import create_metrics

from dfe_engine.hunt_runner.connect import TickGuard, connect_when_available, unavailable_reason
from dfe_engine.hunt_runner.metrics import CLICKHOUSE_UNAVAILABLE, HuntRunnerMetrics
from dfe_engine.settings import ClickHouseResilienceSettings

_AUTH_FAILED = (
    "Code: 516. DB::Exception: dfe_hunt_runner: Authentication failed. (AUTHENTICATION_FAILED)"
)
_UNKNOWN_TABLE = "Code: 60. DB::Exception: Unknown table expression identifier. (UNKNOWN_TABLE)"

_FAST = ClickHouseResilienceSettings(wait_initial=0.01, wait_max=0.04, wait_multiplier=2.0)


def _manager():
    return create_metrics("test", backend="prometheus", enable_auto_update=False)


def _unavailable(manager) -> dict[tuple[str, str], float]:
    counts: dict[tuple[str, str], float] = {}
    for family in text_string_to_metric_families(manager.metrics_text):
        for sample in family.samples:
            if sample.name == CLICKHOUSE_UNAVAILABLE:
                counts[(sample.labels["stage"], sample.labels["reason"])] = sample.value
    return counts


class _Attempts:
    """An attempt that raises each queued error in turn, then returns ``value``."""

    def __init__(self, errors: list[BaseException], value: str = "connected") -> None:
        self._errors = list(errors)
        self._value = value
        self.calls = 0

    def __call__(self) -> str:
        self.calls += 1
        if self._errors:
            raise self._errors.pop(0)
        return self._value


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (Exception(_AUTH_FAILED), "authentication"),
        (
            Exception("Code: 192. DB::Exception: There is no user `x`. (UNKNOWN_USER)"),
            "authentication",
        ),
        (ConnectionRefusedError("[Errno 111] Connection refused"), "connection"),
        (Exception(_UNKNOWN_TABLE), None),
        (ValueError("bad config"), None),
    ],
    ids=["auth-failed", "unknown-user", "outage", "unknown-table", "bad-config"],
)
def test_only_an_outage_or_a_refused_user_is_worth_waiting_out(error, reason):
    assert unavailable_reason(error) == reason


def test_a_refused_login_is_retried_with_back_off_until_the_user_exists():
    """The engine's background reconcile makes the user; the runner is still running when it does."""
    manager = _manager()
    attempts = _Attempts([Exception(_AUTH_FAILED)] * 3 + [ConnectionRefusedError("refused")])
    waits: list[float] = []

    result = connect_when_available(
        attempts,
        resilience=_FAST,
        metrics=HuntRunnerMetrics(manager),
        user="dfe_hunt_runner",
        sleep=waits.append,
    )

    assert result == "connected"
    assert attempts.calls == 5
    assert waits == [0.01, 0.02, 0.04, 0.04]
    assert _unavailable(manager) == {
        ("connect", "authentication"): 3.0,
        ("connect", "connection"): 1.0,
    }


def test_a_failure_waiting_cannot_fix_surfaces_at_once():
    manager = _manager()
    attempts = _Attempts([Exception(_UNKNOWN_TABLE)])
    waits: list[float] = []

    with pytest.raises(Exception, match="UNKNOWN_TABLE"):
        connect_when_available(
            attempts,
            resilience=_FAST,
            metrics=HuntRunnerMetrics(manager),
            user="dfe_hunt_runner",
            sleep=waits.append,
        )

    assert attempts.calls == 1
    assert waits == []
    assert _unavailable(manager) == {}


def test_there_is_no_budget_after_which_the_runner_gives_up():
    """The configured budget bounds a data-plane op; the runner has nothing else to do."""
    attempts = _Attempts([ConnectionRefusedError("refused")] * 50)
    waits: list[float] = []
    short_budget = ClickHouseResilienceSettings(
        wait_initial=0.01, wait_max=0.01, budget_seconds=0.0, waking_budget_seconds=0.0
    )

    result = connect_when_available(
        attempts,
        resilience=short_budget,
        metrics=HuntRunnerMetrics(),
        user="dfe_hunt_runner",
        sleep=waits.append,
    )

    assert result == "connected"
    assert len(waits) == 50


class _Ticks:
    """A tick that raises each queued error in turn, then reports one run."""

    def __init__(self, errors: list[BaseException]) -> None:
        self._errors = list(errors)

    def __call__(self, now: int) -> int:
        if self._errors:
            raise self._errors.pop(0)
        return 1


def test_a_tick_clickhouse_refuses_is_skipped_and_counted_not_fatal():
    manager = _manager()
    guard = TickGuard(
        _Ticks([ConnectionRefusedError("refused"), Exception(_AUTH_FAILED)]),
        metrics=HuntRunnerMetrics(manager),
    )

    assert [guard(now) for now in (1, 2, 3)] == [0, 0, 1]
    assert _unavailable(manager) == {("tick", "connection"): 1.0, ("tick", "authentication"): 1.0}


def test_a_tick_that_fails_for_another_reason_still_raises():
    guard = TickGuard(_Ticks([KeyError("hunt")]), metrics=HuntRunnerMetrics())

    with pytest.raises(KeyError):
        guard(1)
