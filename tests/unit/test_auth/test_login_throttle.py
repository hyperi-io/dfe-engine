#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_login_throttle.py
#  Purpose:      Failed sign-ins back off per username and per client address
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The throttle's counting, waits and memory bound, on an injected clock."""

import pytest

from dfe_engine.auth.login_throttle import LoginThrottle
from dfe_engine.settings import LoginThrottleSettings


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


def _throttle(clock: _Clock, **settings) -> LoginThrottle:
    return LoginThrottle(LoginThrottleSettings(**settings), now=clock)


def _fail(throttle: LoginThrottle, times: int, user: str = "alice", client: str = "10.0.0.1"):
    for _ in range(times):
        throttle.failed(user, client)


class TestTheUsernameCount:
    def test_below_the_threshold_nothing_waits(self, clock):
        throttle = _throttle(clock)
        _fail(throttle, 4)

        assert throttle.retry_after("alice", "10.0.0.1") == 0

    def test_at_the_threshold_the_next_attempt_waits(self, clock):
        throttle = _throttle(clock)
        _fail(throttle, 5)

        assert throttle.retry_after("alice", "10.0.0.2") == 2

    def test_each_further_failure_doubles_the_wait(self, clock):
        throttle = _throttle(clock)
        waits = []
        for _ in range(4):
            _fail(throttle, 1 if waits else 5)
            waits.append(throttle.retry_after("alice", None))
            clock.now += waits[-1]

        assert waits == [2, 4, 8, 16]

    def test_the_wait_stops_at_the_cap(self, clock):
        throttle = _throttle(clock, max_delay_seconds=60)
        _fail(throttle, 40)

        assert throttle.retry_after("alice", None) == 60

    def test_the_wait_lapses(self, clock):
        throttle = _throttle(clock)
        _fail(throttle, 5)
        clock.now += 2

        assert throttle.retry_after("alice", None) == 0

    def test_a_username_is_one_key_whatever_its_case(self, clock):
        throttle = _throttle(clock)
        _fail(throttle, 5, user="Alice")

        assert throttle.retry_after("alice", None) > 0

    def test_a_success_forgets_the_usernames_failures(self, clock):
        throttle = _throttle(clock)
        _fail(throttle, 5)

        throttle.succeeded("alice")

        assert throttle.retry_after("alice", None) == 0

    def test_failures_are_forgotten_after_a_quiet_span(self, clock):
        throttle = _throttle(clock, max_delay_seconds=60)
        _fail(throttle, 4)
        clock.now += 61
        _fail(throttle, 1)

        assert throttle.retry_after("alice", None) == 0


class TestTheClientCount:
    def test_one_address_trying_many_usernames_waits(self, clock):
        throttle = _throttle(clock, client_failures=3)
        for user in ("a", "b", "c"):
            throttle.failed(user, "10.0.0.1")

        assert throttle.retry_after("d", "10.0.0.1") == 2
        assert throttle.retry_after("d", "10.0.0.2") == 0

    def test_a_success_does_not_clear_the_address(self, clock):
        throttle = _throttle(clock, client_failures=3)
        for user in ("a", "b", "c"):
            throttle.failed(user, "10.0.0.1")

        throttle.succeeded("mine")

        assert throttle.retry_after("d", "10.0.0.1") > 0


class TestBounds:
    def test_disabled_it_never_waits(self, clock):
        throttle = _throttle(clock, enabled=False)
        _fail(throttle, 50)

        assert throttle.retry_after("alice", "10.0.0.1") == 0

    def test_memory_holds_a_fixed_number_of_keys(self, clock):
        throttle = LoginThrottle(LoginThrottleSettings(), now=clock, max_keys=10)
        for n in range(100):
            throttle.failed(f"user-{n}", None)

        assert len(throttle._failures) == 10
