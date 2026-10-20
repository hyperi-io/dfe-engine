#  Project:      dfe-engine
#  File:         tests/unit/test_clickhouse/test_schema_retry.py
#  Purpose:      A transient schema bootstrap failure is retried until the pod can go Ready
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""One transient ClickHouse error must not hold the pod NotReady until a restart."""

import threading

import pytest
from clickhouse_connect.driver.exceptions import DatabaseError

from dfe_engine.clickhouse.bootstrap import SchemaRetry
from dfe_engine.schema import phase
from dfe_engine.schema.lock import SchemaLockError
from dfe_engine.schema.manifest_applier import ManifestApplyError
from dfe_engine.schema.phase import (
    STATE_CONVERGED,
    STATE_FAILED,
    STATE_OBSERVED,
    SchemaBootstrapState,
    is_transient_failure,
    run_bootstrap,
    schema_ready,
)
from dfe_engine.schema.plan import SchemaPlanError
from dfe_engine.settings import ClickHouseResilienceSettings, ClickHouseSettings, DFESettings

FAST = ClickHouseResilienceSettings(wait_initial=0.001, wait_max=0.001)

MEMORY_LIMIT = "Code: 241. DB::Exception: Memory limit (total) exceeded: would use 30.01 GiB"


@pytest.fixture(autouse=True)
def _restore_state():
    before = phase.current_state()
    yield
    phase._set_state(before)


def _caused_by(outer: Exception, cause: BaseException) -> Exception:
    outer.__cause__ = cause
    return outer


class TestWhatAnotherPassCanClear:
    @pytest.mark.parametrize(
        "exc",
        [
            _caused_by(SchemaLockError("could not write the lease"), DatabaseError(MEMORY_LIMIT)),
            _caused_by(
                ManifestApplyError("ClickHouse did not answer"),
                ConnectionRefusedError(111, "Connection refused"),
            ),
            DatabaseError("Code: 202. DB::Exception: Too many simultaneous queries"),
        ],
        ids=["memory-limit-on-the-lock", "connection-refused", "too-many-queries"],
    )
    def test_load_and_reachability_are_transient(self, exc):
        assert is_transient_failure(exc) is True

    @pytest.mark.parametrize(
        "exc",
        [
            SchemaPlanError("the manifest names a table twice"),
            _caused_by(
                ManifestApplyError("refused"),
                DatabaseError("Code: 62. DB::Exception: Syntax error"),
            ),
        ],
        ids=["bad-manifest", "syntax-error"],
    )
    def test_a_fault_no_pass_can_clear_is_not(self, exc):
        assert is_transient_failure(exc) is False

    def test_the_pass_records_a_transient_failure(self, monkeypatch):
        def refused(settings, *, wait_seconds):
            raise ManifestApplyError("ClickHouse did not answer") from ConnectionRefusedError(
                111, "Connection refused"
            )

        monkeypatch.setattr(phase, "_connect", refused)
        settings = DFESettings(env="dev", clickhouse=ClickHouseSettings(bootstrap_tables=True))

        state = run_bootstrap(settings=settings, wait_seconds=0)

        assert (state.state, state.transient) == (STATE_FAILED, True)
        assert "transient" not in state.as_dict()
        assert schema_ready() is False


def _passes(outcomes: list[SchemaBootstrapState], ready_before: list[bool]):
    """A pass that reports each outcome in turn, as the real pass does, and notes readiness first."""
    remaining = list(outcomes)

    def run_pass() -> SchemaBootstrapState:
        ready_before.append(schema_ready())
        state = remaining.pop(0)
        phase._set_state(state)
        return state

    return run_pass


def _transient() -> SchemaBootstrapState:
    return SchemaBootstrapState(state=STATE_FAILED, error="lock write refused", transient=True)


def _observed() -> SchemaBootstrapState:
    """What a pass reports when another applier held the lease for its whole wait."""
    return SchemaBootstrapState(state=STATE_OBSERVED, holder="pod-a", transient=True)


class TestSchemaRetry:
    def test_a_bootstrap_that_fails_twice_then_succeeds_becomes_ready(self):
        phase._set_state(_transient())
        ready_before: list[bool] = []
        converged = threading.Event()
        retry = SchemaRetry(
            _passes(
                [_transient(), _transient(), SchemaBootstrapState(state=STATE_CONVERGED)],
                ready_before,
            ),
            resilience=FAST,
            on_converged=converged.set,
        )

        retry.start()
        assert converged.wait(10), "the retry never converged"
        retry.close(10)

        assert ready_before == [False, False, False]
        assert schema_ready() is True

    def test_a_pass_that_outwaited_another_holders_lease_runs_again_not_ready(self):
        phase._set_state(_observed())
        ready_before: list[bool] = []
        converged = threading.Event()
        retry = SchemaRetry(
            _passes([_observed(), SchemaBootstrapState(state=STATE_CONVERGED)], ready_before),
            resilience=FAST,
            on_converged=converged.set,
        )

        final = retry.run()

        assert final is not None
        assert final.state == STATE_CONVERGED
        assert ready_before == [False, False]
        assert converged.is_set()
        assert schema_ready() is True

    def test_a_failure_no_pass_can_clear_ends_the_retry(self):
        phase._set_state(_transient())
        ready_before: list[bool] = []
        permanent = SchemaBootstrapState(state=STATE_FAILED, error="manifest", transient=False)
        converged: list[bool] = []
        retry = SchemaRetry(
            _passes([permanent], ready_before),
            resilience=FAST,
            on_converged=lambda: converged.append(True),
        )

        assert retry.run() is permanent
        assert (ready_before, converged, schema_ready()) == ([False], [], False)

    def test_close_stops_a_retry_waiting_out_its_back_off(self):
        ready_before: list[bool] = []
        retry = SchemaRetry(
            _passes([], ready_before),
            resilience=ClickHouseResilienceSettings(wait_initial=60.0, wait_max=60.0),
        )

        retry.start()
        retry.close(5)

        assert not retry._thread.is_alive()
        assert ready_before == []

    def test_resilience_switched_off_runs_one_more_pass(self):
        ready_before: list[bool] = []
        still_failing = _transient()
        retry = SchemaRetry(
            _passes([still_failing], ready_before),
            resilience=ClickHouseResilienceSettings(enabled=False, wait_initial=0.001),
        )

        assert retry.run() is still_failing
        assert len(ready_before) == 1
