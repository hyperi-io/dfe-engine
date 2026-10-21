#  Project:      dfe-engine
#  File:         tests/integration/test_source_deploy_lease_ch.py
#  Purpose:      A source deploy's DDL takes the schema lease the boot phase takes
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The deploy route against a real ClickHouse while another applier holds the schema lease.

The boot phase and a source deploy both ALTER tables in the data database, so the
deploy waits for the lease rather than interleaving its DDL with a held apply.
"""

import threading
import time

import pytest

from dfe_engine.schema.lock import SchemaLock, holder_name
from dfe_engine.schema.plan import LOCK_ID, render_one

pytestmark = pytest.mark.integration

_HELD_SECONDS = 4.0


def _create_source(stack, name: str) -> None:
    created = stack.client.post(
        "/api/v1/sources",
        headers=stack.headers,
        json={
            "display_name": name,
            "enabled": True,
            "header": {"type": "timeseries", "version": "1.0.0"},
            "match": {"field": "tags.collector.type", "value": name},
            "schema": {"meta_schema": "meta/syslog"},
            "source": name,
        },
    )
    assert created.status_code in (200, 201), created.text


def _held_by(ch_client, database: str, holder: str) -> SchemaLock:
    lock = SchemaLock(
        ch_client,
        database=database,
        table=render_one(LOCK_ID, data_database=database).name,
        holder=holder,
        lease_seconds=300,
    )
    # A claim held up by a server stall is a missed attempt, so taking the lease here waits.
    assert lock.acquire(wait_seconds=10.0) is True
    return lock


def _table_exists(client, database: str, table: str) -> bool:
    rows = client.query(
        "SELECT 1 FROM system.tables WHERE database = {db:String} AND name = {tbl:String}",
        parameters={"db": database, "tbl": table},
    ).result_rows
    return bool(rows)


def test_a_source_deploy_waits_for_the_schema_lease(ch_client, stack):
    name = "syslog-leased"
    _create_source(stack, name)
    holder = _held_by(ch_client, stack.database, "pod-a")
    seen_while_held: list[bool] = []

    def finish_the_holders_apply() -> None:
        time.sleep(_HELD_SECONDS)
        seen_while_held.append(_table_exists(ch_client, stack.database, name))
        holder.release()

    releaser = threading.Thread(target=finish_the_holders_apply)
    started = time.monotonic()
    releaser.start()
    deployed = stack.client.post(f"/api/v1/sources/{name}/deploy", headers=stack.headers)
    elapsed = time.monotonic() - started
    releaser.join()

    assert deployed.status_code == 200, deployed.text
    assert seen_while_held == [False], "the deploy's DDL ran while another applier held the lease"
    assert elapsed >= _HELD_SECONDS
    assert _table_exists(ch_client, stack.database, name)
    latest = holder.read()
    assert latest is not None
    assert latest.released is True
    assert latest.holder.startswith(f"{holder_name()}/"), latest.holder


def test_a_source_deploy_that_outwaits_the_lease_answers_503_and_applies_nothing(
    ch_client, stack, monkeypatch
):
    name = "syslog-locked"
    _create_source(stack, name)
    monkeypatch.setattr(stack.client.app.state.settings.clickhouse, "bootstrap_wait_seconds", 0.0)
    _held_by(ch_client, stack.database, "pod-a")

    deployed = stack.client.post(f"/api/v1/sources/{name}/deploy", headers=stack.headers)

    assert deployed.status_code == 503, deployed.text
    assert deployed.json()["code"] == "schema_locked"
    assert not _table_exists(ch_client, stack.database, name)
