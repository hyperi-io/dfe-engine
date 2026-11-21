#  Project:      dfe-engine
#  File:         tests/integration/test_system_retention_ch.py
#  Purpose:      An admin's default TTL reaches the live tables on a real ClickHouse
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""PUT /api/v1/system/retention against a real ClickHouse, through the real API.

The unit suite proves who may set the default and what a PUT accepts. Whether the
TTL on a live table actually moved is a question only the server answers, so here
the core tables come from the manifest apply, a source is created and deployed
through the API, and every assertion reads ``system.tables``. A PUT stores the value
and returns, so each test waits for the pass it asked for before it reads a table.
"""

import pytest

from dfe_engine.gitcrud.retention import effective_settings
from dfe_engine.schema.phase import apply_plan
from dfe_engine.schema.plan import build_plan
from tests.support.engine_stack import converged, deploy_source, live_ttl_days

pytestmark = [pytest.mark.integration, pytest.mark.own_clickhouse]

URL = "/api/v1/system/retention"


def test_an_admin_change_moves_every_table_that_follows_the_default(ch_client, stack):
    deploy_source(name="syslog-before", stack=stack)
    db = stack.database
    assert live_ttl_days(ch_client, db, "main") == 90
    assert live_ttl_days(ch_client, db, "syslog-before") == 90
    assert live_ttl_days(ch_client, db, "detection_checkpoint") == 30

    resp = stack.client.put(URL, headers=stack.headers, json={"default_ttl_days": 45})

    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "default_ttl_days": 45,
        "stored": 45,
        "origin": "override",
        "deployment_default": 90,
        "editable": True,
    }
    converged(stack)

    for table in ("main", "detection", "otel_logs", "otel_traces", "syslog-before"):
        assert live_ttl_days(ch_client, db, table) == 45, table
    # Declares its own TTL in dfe-schemas, so the default never governs it.
    assert live_ttl_days(ch_client, db, "detection_checkpoint") == 30

    assert stack.crud.get("gov_settings", "retention")["default_ttl_days"] == 45
    assert stack.client.get(URL, headers=stack.headers).json()["default_ttl_days"] == 45


def test_a_source_built_or_deployed_after_the_change_takes_it(ch_client, stack):
    assert (
        stack.client.put(URL, headers=stack.headers, json={"default_ttl_days": 45}).status_code
        == 200
    )
    converged(stack)

    deploy_source(name="syslog-after", stack=stack)
    built = stack.client.post("/api/v1/sources/syslog-after/build", headers=stack.headers)

    assert live_ttl_days(ch_client, stack.database, "syslog-after") == 45
    assert built.status_code == 200, built.text
    assert "INTERVAL 45 DAY" in built.json()["ddl"]["create_table"]


def test_zero_removes_the_ttl_and_clearing_restores_the_deployment_default(ch_client, stack):
    deploy_source(name="syslog-zero", stack=stack)
    db = stack.database
    # The stored build a console's plan step leaves behind carries no columns, and the
    # TTL has to be placed over one. The pass must not lean on it.
    stored = stack.client.post("/api/v1/sources/syslog-zero/build", headers=stack.headers)
    assert stored.status_code == 200, stored.text

    zero = stack.client.put(URL, headers=stack.headers, json={"default_ttl_days": 0})

    assert zero.status_code == 200, zero.text
    converged(stack)
    assert live_ttl_days(ch_client, db, "main") is None
    assert live_ttl_days(ch_client, db, "syslog-zero") is None
    assert live_ttl_days(ch_client, db, "detection_checkpoint") == 30

    cleared = stack.client.put(URL, headers=stack.headers, json={"default_ttl_days": None})

    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["origin"] == "deployment"
    assert cleared.json()["stored"] is None
    assert cleared.json()["default_ttl_days"] == 90
    converged(stack)
    assert live_ttl_days(ch_client, db, "main") == 90
    assert live_ttl_days(ch_client, db, "syslog-zero") == 90


def test_the_next_boot_renders_the_override_so_nothing_reads_as_drift(ch_client, stack):
    put = stack.client.put(URL, headers=stack.headers, json={"default_ttl_days": 45})
    assert put.status_code == 200, put.text
    converged(stack)

    settings = effective_settings(stack.client.app.state.settings, stack.crud)
    report = apply_plan(ch_client, build_plan(settings=settings, client=ch_client))

    assert settings.clickhouse.default_ttl_days == 45
    assert not report.refused, [outcome.describe() for outcome in report.refused]
    assert live_ttl_days(ch_client, stack.database, "main") == 45


def test_a_plan_from_the_deployment_settings_reads_the_override_as_drift(ch_client, stack):
    """`dfe schema plan` and `apply` build from the deployment settings, which know no console value."""
    put = stack.client.put(URL, headers=stack.headers, json={"default_ttl_days": 45})
    assert put.status_code == 200, put.text
    converged(stack)
    plan = build_plan(settings=stack.client.app.state.settings, client=ch_client)

    planned = apply_plan(ch_client, plan, dry_run=True)

    assert any(
        f"{stack.database}.main" in outcome.describe() and "TTL is 45 days" in outcome.describe()
        for outcome in planned.refused
    ), [outcome.describe() for outcome in planned.refused]
    assert live_ttl_days(ch_client, stack.database, "main") == 45

    forced = apply_plan(ch_client, plan, allow_drift=True)

    assert not forced.refused, [outcome.describe() for outcome in forced.refused]
    assert live_ttl_days(ch_client, stack.database, "main") == 90
