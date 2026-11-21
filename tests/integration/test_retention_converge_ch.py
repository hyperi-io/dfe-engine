#  Project:      dfe-engine
#  File:         tests/integration/test_retention_converge_ch.py
#  Purpose:      The default TTL converges on a real ClickHouse to the newest stored value
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""One pass brings every table that follows the default TTL to the committed value.

A PUT stores the value and returns, so the tables only reach it if the loop that runs
afterwards, and at every start, cannot lose it. Each test here is one way it could:

- a pass longer than one lease, so a peer takes the lease over mid-pass
- an override the pass cannot read, which a fall-back to the deployment default would
  turn into a shorter TTL than was stored
- a burst of changes, where only the newest may survive
- a lease another applier holds, so the pass cannot start
- a removal that was committed and never applied
- a table ClickHouse refuses

Every assertion reads ``system.tables`` on a real server.
"""

import functools
import threading
import time
from collections.abc import Callable
from pathlib import Path

import clickhouse_connect
import pytest

from dfe_engine.api.deps import get_clickhouse_client, get_source_registry_optional
from dfe_engine.gitcrud.retention import (
    RetentionUnreadable,
    effective_settings,
    resolve_state,
    set_stored,
    with_default_ttl_days,
)
from dfe_engine.governance.ch import ReconcileTrigger
from dfe_engine.schema.lock import (
    HeldLease,
    SchemaLeaseLostError,
    SchemaLock,
    SchemaLockBusyError,
    renewed_lease,
)
from dfe_engine.schema.plan import LOCK_ID, render_one
from dfe_engine.schema.retention import (
    ConvergePass,
    converge_default_ttl,
    reconcile_core_ttls,
    reconcile_source_ttls,
)
from tests.support.engine_stack import (
    converged,
    deploy_source,
    hold_schema_lease,
    live_ttl_days,
)

pytestmark = [pytest.mark.integration, pytest.mark.own_clickhouse]

URL = "/api/v1/system/retention"
# Engine tables that follow the default. detection_checkpoint declares its own 30 days.
FOLLOWERS = ("main", "detection", "otel_logs", "otel_traces")
DEPLOYMENT_DEFAULT = 90


def _trigger(run: Callable[[], ConvergePass]) -> ReconcileTrigger:
    """The loop the engine runs, with waits short enough that a retry costs milliseconds and not 5 s."""
    return ReconcileTrigger(
        run,
        reconcile="retention",
        settle_seconds=0.05,
        retry_initial_seconds=0.2,
        retry_max_seconds=0.5,
    )


def _pass(stack) -> ConvergePass:
    """One pass as the engine runs it, over the stack's own deploy repo and registry."""
    settings = stack.app.state.settings
    registry = get_source_registry_optional()
    return converge_default_ttl(
        get_clickhouse_client(settings),
        settings=settings,
        crud=stack.crud,
        load_sources=registry.get_all_sources,
    )


def _all_at(ch_client, stack, days: int | None, *sources: str) -> None:
    for table in (*FOLLOWERS, *sources):
        assert live_ttl_days(ch_client, stack.database, table) == days, table


def test_a_pass_extends_the_lease_before_each_table_it_alters(ch_client, stack):
    deploy_source(name="syslog-a", stack=stack)
    deploy_source(name="syslog-b", stack=stack)
    set_stored(stack.crud, 45, "test")

    result = _pass(stack)

    assert result.days == 45
    assert result.outcome.sources_reconciled == 2
    assert result.outcome.sources_skipped == 0
    assert result.errors == []
    assert len(result.outcome.core_altered) >= len(FOLLOWERS)
    # Once before each table and once after the last, so a pass of any length keeps its lease.
    tables = len(result.outcome.core_altered) + result.outcome.sources_reconciled
    assert result.renewals == tables + 1
    _all_at(ch_client, stack, 45, "syslog-a", "syslog-b")
    assert live_ttl_days(ch_client, stack.database, "detection_checkpoint") == 30


def test_a_lease_renewed_outlasts_its_length_and_one_left_to_lapse_is_taken_over(ch_client, stack):
    table = render_one(LOCK_ID, data_database=stack.database).name

    def lock(holder: str) -> SchemaLock:
        return SchemaLock(
            ch_client, database=stack.database, table=table, holder=holder, lease_seconds=3
        )

    holder, rival = lock("pod-a"), lock("pod-b")
    assert holder.acquire(wait_seconds=10.0) is True

    # Five renewals 0.8 s apart is 4 s against a 3 s lease.
    for _ in range(5):
        time.sleep(0.8)
        assert holder.renew() is True
        assert rival.acquire(wait_seconds=0.0) is False

    time.sleep(4.5)

    assert rival.acquire(wait_seconds=10.0) is True
    assert holder.renew() is False
    with pytest.raises(SchemaLeaseLostError):
        HeldLease(holder).renew()


def test_a_server_without_the_lease_table_runs_the_block_unheld(
    ch_client, clickhouse_test_database
):
    with renewed_lease(ch_client, data_database=clickhouse_test_database, wait_seconds=1.0) as held:
        held.renew()

    assert held.renewals == 0


def test_an_override_that_cannot_be_read_alters_nothing_until_it_can(ch_client, stack):
    deploy_source(name="syslog-read", stack=stack)
    set_stored(stack.crud, 45, "test")
    _pass(stack)
    _all_at(ch_client, stack, 45, "syslog-read")
    override = stack.crud.repo_path / "governance" / "settings" / "retention.yaml"

    override.write_text("[not: a mapping", encoding="utf-8")

    # A read for display falls back to the deployment default, which a pass must not do.
    assert resolve_state(stack.crud, stack.app.state.settings).effective == DEPLOYMENT_DEFAULT
    with pytest.raises(RetentionUnreadable):
        _pass(stack)
    _all_at(ch_client, stack, 45, "syslog-read")

    override.write_text("default_ttl_days: 30\n", encoding="utf-8")

    assert _pass(stack).days == 30
    _all_at(ch_client, stack, 30, "syslog-read")


def test_a_burst_of_changes_ends_on_the_newest_stored_value(ch_client, stack):
    deploy_source(name="syslog-burst", stack=stack)
    trigger = _trigger(functools.partial(_pass, stack))

    try:
        # Each request lands while the one before may still be applying, and
        # none can be dropped: a request is a flag, not a place in a queue.
        for days in range(10, 40):
            set_stored(stack.crud, days, "test")
            trigger.request()
        assert trigger.wait_idle(120.0)
    finally:
        trigger.close(10.0)

    _all_at(ch_client, stack, 39, "syslog-burst")


def test_a_pass_that_cannot_get_the_lease_runs_again_once_it_is_free(ch_client, stack, monkeypatch):
    deploy_source(name="syslog-leased", stack=stack)
    monkeypatch.setattr(stack.app.state.settings.clickhouse, "bootstrap_wait_seconds", 1.0)
    set_stored(stack.crud, 45, "test")
    holder = hold_schema_lease(ch_client, stack.database, "pod-a")

    with pytest.raises(SchemaLockBusyError):
        _pass(stack)
    _all_at(ch_client, stack, DEPLOYMENT_DEFAULT, "syslog-leased")

    attempts: list[str] = []

    def run() -> ConvergePass:
        try:
            result = _pass(stack)
        except SchemaLockBusyError:
            attempts.append("busy")
            raise
        attempts.append("applied")
        return result

    trigger = _trigger(run)
    releaser = threading.Timer(2.5, holder.release)
    releaser.start()
    try:
        trigger.request()
        assert trigger.wait_idle(60.0)
    finally:
        releaser.join()
        trigger.close(10.0)

    assert attempts[0] == "busy"
    assert attempts[-1] == "applied"
    _all_at(ch_client, stack, 45, "syslog-leased")


def test_a_removal_that_was_never_applied_is_applied_by_the_next_pass(ch_client, stack):
    deploy_source(name="syslog-removed", stack=stack)
    assert (
        stack.client.put(URL, headers=stack.headers, json={"default_ttl_days": 45}).status_code
        == 200
    )
    converged(stack)
    _all_at(ch_client, stack, 45, "syslog-removed")

    # The removal is committed and the process ends before it asks for a pass.
    set_stored(stack.crud, None, "test")
    _all_at(ch_client, stack, 45, "syslog-removed")

    result = _pass(stack)

    assert result.days == DEPLOYMENT_DEFAULT
    _all_at(ch_client, stack, DEPLOYMENT_DEFAULT, "syslog-removed")
    assert live_ttl_days(ch_client, stack.database, "detection_checkpoint") == 30


# Three starts, each running the schema phase, so the suite's 300 s default is too short.
@pytest.mark.timeout(900)
def test_a_restart_keeps_the_stored_value_and_applies_a_removal_made_while_down(
    ch_client, engine_restarts
):
    with engine_restarts() as first:
        assert (
            first.client.put(URL, headers=first.headers, json={"default_ttl_days": 45}).status_code
            == 200
        )
        converged(first)
        _all_at(ch_client, first, 45)

    with engine_restarts() as second:
        converged(second)
        # The deployment default is 90, and the stored 45 is what the tables keep.
        _all_at(ch_client, second, 45)
        set_stored(second.crud, None, "test")

    with engine_restarts() as third:
        converged(third)
        _all_at(ch_client, third, DEPLOYMENT_DEFAULT)
        assert live_ttl_days(ch_client, third.database, "detection_checkpoint") == 30


def test_a_source_that_does_not_build_is_retried_until_it_does(ch_client, stack):
    deploy_source(name="syslog-unbuilt", stack=stack)
    set_stored(stack.crud, 45, "test")
    meta = Path(stack.app.state.settings.schemas.schemas_dir) / "meta" / "syslog.yaml"
    schema = meta.read_text(encoding="utf-8")
    meta.unlink()

    unbuilt = _pass(stack)

    assert unbuilt.errors == [f"{stack.database}.syslog-unbuilt"]
    assert live_ttl_days(ch_client, stack.database, "syslog-unbuilt") == DEPLOYMENT_DEFAULT
    _all_at(ch_client, stack, 45)

    meta.write_text(schema, encoding="utf-8")

    assert _pass(stack).errors == []
    _all_at(ch_client, stack, 45, "syslog-unbuilt")


def test_a_table_clickhouse_refuses_is_reported_and_taken_once_it_will(ch_client, ch_params, stack):
    deploy_source(name="syslog-refused", stack=stack)
    database = stack.database
    settings = with_default_ttl_days(effective_settings(stack.app.state.settings, stack.crud), 45)
    # The engine's own tables take the value as the admin does, leaving only the source.
    reconcile_core_ttls(ch_client, settings=settings)
    registry = get_source_registry_optional()
    ch_client.command("CREATE USER IF NOT EXISTS ttl_reader IDENTIFIED WITH no_password")
    # Reads the catalogue and the table and refuses the ALTER, as a reader account would.
    ch_client.command(f"GRANT SELECT ON `{database}`.*, SELECT ON system.* TO ttl_reader")
    reader = clickhouse_connect.get_client(
        host=ch_params["host"],
        port=ch_params["port"],
        username="ttl_reader",
        password="",
        secure=ch_params["secure"],
    )
    try:
        refused = reconcile_source_ttls(
            reader, settings=settings, sources=registry.get_all_sources()
        )

        assert refused.failed == [f"{database}.syslog-refused"]
        assert refused.sources_skipped == 1
        assert live_ttl_days(ch_client, database, "syslog-refused") == DEPLOYMENT_DEFAULT

        ch_client.command(f"GRANT ALTER ON `{database}`.* TO ttl_reader")
        taken = reconcile_source_ttls(reader, settings=settings, sources=registry.get_all_sources())

        assert taken.failed == []
        assert live_ttl_days(ch_client, database, "syslog-refused") == 45
    finally:
        reader.close()
        ch_client.command("DROP USER IF EXISTS ttl_reader")
