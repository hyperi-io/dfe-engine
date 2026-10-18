#  Project:      dfe-engine
#  File:         tests/integration/test_system_defaults_apply_ch.py
#  Purpose:      Applying the table defaults moves deployed tables on a real ClickHouse
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""POST /system/defaults/apply and GET /system/defaults/drift against a real ClickHouse.

The unit suite proves what apply pins and what drift reports for sources with no
table. Whether a deployed table actually took the pinned TTL and header, kept its
engine, and reads that way in the drift report is a question only the server
answers, so every source here is deployed through the API and every table
assertion reads ``system.tables`` or ``system.columns``.

Apply never rewrites a deployed version: it pins a new one, and the table takes
the values when that version is deployed. A deployed version that already carries
the values has its table brought to them by apply itself.
"""

import pytest
from scalo.logger import logger

from dfe_engine.gitcrud.table_defaults import commit_patch
from dfe_engine.schema.schema_loader import SchemaLoader
from tests.support.engine_stack import deploy_source

pytestmark = pytest.mark.integration

DEFAULTS = "/api/v1/system/defaults"
APPLY = "/api/v1/system/defaults/apply"
DRIFT = "/api/v1/system/defaults/drift"
REBUILD = "needs a table rebuild; applies to new tables only"
REFUSED = "ClickHouse could not be read or refused the change; the engine log has why"
# A schema already on the deployment's table defaults, so apply pins nothing new on it.
ON_THE_DEFAULTS = {"meta_schema": "meta/syslog", "ttl_days": 90, "engine": "MergeTree"}


def _table(ch_client, database: str, table: str) -> tuple[str, str]:
    rows = ch_client.query(
        "SELECT engine, engine_full FROM system.tables "
        "WHERE database = {db:String} AND name = {t:String}",
        parameters={"db": database, "t": table},
    ).result_rows
    assert rows, f"{database}.{table} does not exist"
    return str(rows[0][0]), str(rows[0][1])


def _live_ttl_days(ch_client, database: str, table: str) -> int | None:
    _, engine_full = _table(ch_client, database, table)
    _, sep, clause = engine_full.partition(" TTL ")
    if not sep:
        return None
    _, sep, rest = clause.partition("toIntervalDay(")
    return int(rest.split(")", 1)[0]) if sep else None


def _columns(ch_client, database: str, table: str) -> dict[str, str]:
    rows = ch_client.query(
        "SELECT name, type FROM system.columns WHERE database = {db:String} AND table = {t:String}",
        parameters={"db": database, "t": table},
    ).result_rows
    return {str(name): str(ch_type) for name, ch_type in rows}


def _deploy(stack, name: str) -> dict:
    resp = stack.client.post(f"/api/v1/sources/{name}/deploy", headers=stack.headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _apply(stack, *names: str) -> dict:
    resp = stack.client.post(APPLY, headers=stack.headers, json={"sources": list(names)})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _drift(stack) -> dict[str, dict]:
    resp = stack.client.get(DRIFT, headers=stack.headers, params={"per_page": -1})
    assert resp.status_code == 200, resp.text
    return {item["source"]: item for item in resp.json()["items"]}


def _patch(stack, body: dict) -> None:
    resp = stack.client.patch(DEFAULTS, headers=stack.headers, json=body)
    assert resp.status_code == 200, resp.text


def test_apply_pins_a_new_version_and_its_deploy_moves_the_ttl(ch_client, stack):
    db = stack.database
    deploy_source(
        name="syslog-ttl", schema={"meta_schema": "meta/syslog", "ttl_days": 90}, stack=stack
    )
    # The default reconcile leaves a table whose source pins its own TTL alone.
    _patch(stack, {"ttl_days": 91})
    assert _live_ttl_days(ch_client, db, "syslog-ttl") == 90

    body = _apply(stack, "syslog-ttl")

    assert _live_ttl_days(ch_client, db, "syslog-ttl") == 90
    assert body["updated"] == [{"source": "syslog-ttl", "version": "2.0.0"}]
    assert body["live"] == [
        {
            "source": "syslog-ttl",
            "status": "not_deployed",
            "table": f"{db}.syslog-ttl",
            "ttl": None,
            "columns_added": [],
            "not_applied": [],
            "reason": (
                "version 2.0.0 carries these values and version 1.0.0 is deployed; "
                "deploying 2.0.0 applies them"
            ),
        }
    ]
    assert _drift(stack)["syslog-ttl"]["ttl_days"] == {"stored": 91, "default": 91, "live": 90}

    deployed = _deploy(stack, "syslog-ttl")

    assert _live_ttl_days(ch_client, db, "syslog-ttl") == 91
    assert deployed["ttl_change"]["move"] == "90 -> 91"
    assert "syslog-ttl" not in _drift(stack)


def test_drift_reads_the_ttl_the_table_runs_not_the_one_the_source_stores(ch_client, stack):
    db = stack.database
    _patch(stack, {"ttl_days": 91})
    deploy_source(
        name="syslog-moved", schema={"meta_schema": "meta/syslog", "ttl_days": 91}, stack=stack
    )
    ch_client.command(
        f"ALTER TABLE `{db}`.`syslog-moved` MODIFY TTL _timestamp_load + INTERVAL 90 DAY"
    )

    drift = _drift(stack)["syslog-moved"]

    assert drift["drifted"] == ["ttl_days"]
    assert drift["ttl_days"] == {"stored": 91, "default": 91, "live": 90}


def test_deploying_the_applied_header_adds_its_columns_to_the_table(ch_client, stack):
    db = stack.database
    deploy_source(
        header={"type": "minimal", "version": "1.0.0"}, name="syslog-minimal", stack=stack
    )
    before = set(_columns(ch_client, db, "syslog-minimal"))
    header = {col.name for col in SchemaLoader.load_profile("timeseries", profile_version="1.0.0")}
    missing = header - before
    assert {"_timestamp_received", "_tags"} <= missing

    body = _apply(stack, "syslog-minimal")

    assert set(_columns(ch_client, db, "syslog-minimal")) == before
    assert body["updated"] == [{"source": "syslog-minimal", "version": "2.0.0"}]
    assert body["live"][0]["status"] == "not_deployed"

    _deploy(stack, "syslog-minimal")

    assert set(_columns(ch_client, db, "syslog-minimal")) - before == missing
    assert "syslog-minimal" not in _drift(stack)


def test_an_engine_change_needs_a_rebuild_and_stays_drift(ch_client, stack):
    db = stack.database
    deploy_source(name="syslog-engine", stack=stack)
    _patch(stack, {"engine": "ReplacingMergeTree"})
    staged = _apply(stack, "syslog-engine")
    assert staged["updated"] == [{"source": "syslog-engine", "version": "2.0.0"}]
    _deploy(stack, "syslog-engine")

    # The deployed version now carries the engine, so apply goes to the table.
    body = _apply(stack, "syslog-engine")

    engine, _ = _table(ch_client, db, "syslog-engine")
    assert engine == "ReplicatedMergeTree"
    drift = _drift(stack)["syslog-engine"]
    assert drift["drifted"] == ["engine"]
    assert drift["engine"] == {
        "stored": "ReplacingMergeTree",
        "default": "ReplacingMergeTree",
        "live": "MergeTree",
    }
    assert drift["ttl_days"]["live"] == 90
    assert body["unchanged"] == [{"source": "syslog-engine", "version": "2.0.0"}]
    live = body["live"][0]
    assert live["status"] == "unchanged"
    assert live["not_applied"] == [{"field": "engine", "reason": REBUILD}]


def test_an_existing_header_column_keeps_its_nullability(ch_client, stack):
    db = stack.database
    deploy_source(name="syslog-nullable", schema=ON_THE_DEFAULTS, stack=stack)
    # The header declares _uuid Nullable(UUID); the table now holds it non-nullable.
    ch_client.command(f"ALTER TABLE `{db}`.`syslog-nullable` MODIFY COLUMN `_uuid` UUID")
    retyped = {"_uuid"}
    before = _columns(ch_client, db, "syslog-nullable")

    body = _apply(stack, "syslog-nullable")

    assert _columns(ch_client, db, "syslog-nullable")["_uuid"] == before["_uuid"]
    assert body["unchanged"] == [{"source": "syslog-nullable", "version": "1.0.0"}]
    live = body["live"][0]
    assert live["status"] == "unchanged"
    [kept] = live["not_applied"]
    assert kept["field"] == "common_header_version"
    _, _, named = kept["reason"].partition(": ")
    assert set(named.split(", ")) == retyped


def test_plan_and_dry_run_show_the_ttl_move_and_that_it_deletes_rows(ch_client, stack):
    db = stack.database
    deploy_source(name="syslog-plan", stack=stack)
    # Stored without the reconcile a PATCH runs, so the table keeps its 90 days.
    commit_patch(stack.crud, actor="test", ttl_days=30)

    plan = stack.client.post("/api/v1/sources/syslog-plan/plan", headers=stack.headers)
    dry = stack.client.post(
        "/api/v1/sources/syslog-plan/deploy", headers=stack.headers, params={"dry_run": "true"}
    )

    assert _live_ttl_days(ch_client, db, "syslog-plan") == 90
    assert plan.status_code == 200, plan.text
    change = plan.json()["ttl_change"]
    assert change is not None
    assert change["move"] == "90 -> 30"
    assert change["expires_rows"] is True
    assert "MODIFY TTL" in change["statement"]
    assert change["statement"] in plan.json()["statements"]
    assert "rows past the new TTL are deleted" in plan.json()["ready_reason"]
    assert dry.status_code == 200, dry.text
    assert dry.json()["ttl_change"] == change
    assert dry.json()["live_table_error"] is None


def test_a_header_named_by_its_registry_path_is_not_drift(ch_client, stack):
    deploy_source(
        header={"type": "common-header/timeseries", "version": "1.0.0"},
        name="syslog-path",
        stack=stack,
    )

    assert "syslog-path" not in _drift(stack)


def test_one_table_clickhouse_refuses_does_not_hide_the_others(ch_client, stack):
    db = stack.database
    _patch(stack, {"ttl_days": 91})
    schema = {**ON_THE_DEFAULTS, "ttl_days": 91}
    deploy_source(name="syslog-good", schema=schema, stack=stack)
    deploy_source(name="syslog-bad", schema=schema, stack=stack)
    ch_client.command(
        f"ALTER TABLE `{db}`.`syslog-good` MODIFY TTL _timestamp_load + INTERVAL 90 DAY"
    )
    # A view under the deployed table's name: every ALTER on it is refused.
    ch_client.command(f"DROP TABLE `{db}`.`syslog-bad` SYNC")
    ch_client.command(f"CREATE VIEW `{db}`.`syslog-bad` AS SELECT 1 AS x")

    captured: list = []
    handler_id = logger.add(captured.append, level="WARNING")
    try:
        body = _apply(stack, "syslog-bad", "syslog-good")
    finally:
        logger.remove(handler_id)

    assert _live_ttl_days(ch_client, db, "syslog-good") == 91
    assert body["updated"] == []
    assert body["unchanged"] == [
        {"source": "syslog-bad", "version": "1.0.0"},
        {"source": "syslog-good", "version": "1.0.0"},
    ]
    bad, good = body["live"]
    assert bad["source"] == "syslog-bad"
    assert bad["status"] == "failed"
    assert bad["reason"] == REFUSED
    # The reason is the same for a failed read, so the log shows ClickHouse refused a statement.
    refusals = [
        m.record["message"] for m in captured if "table defaults not applied" in m.record["message"]
    ]
    assert len(refusals) == 1, refusals
    assert refusals[0].startswith(f"{db}.syslog-bad: table defaults not applied: ")
    assert f"ClickHouse rejected: ALTER TABLE `{db}`.`syslog-bad`" in refusals[0]
    assert good["source"] == "syslog-good"
    assert good["status"] == "altered"
    assert good["ttl"] == "90 -> 91"


def test_a_redeploy_moves_an_existing_tables_ttl(ch_client, stack):
    db = stack.database
    deploy_source(name="syslog-redeploy", stack=stack)
    assert _live_ttl_days(ch_client, db, "syslog-redeploy") == 90
    # Stored without the reconcile a PATCH runs, so only the deploy can move the table.
    commit_patch(stack.crud, actor="test", ttl_days=60)

    resp = stack.client.post("/api/v1/sources/syslog-redeploy/deploy", headers=stack.headers)

    assert resp.status_code == 200, resp.text
    assert _live_ttl_days(ch_client, db, "syslog-redeploy") == 60
    assert resp.json()["statements_applied"] >= 1
    assert resp.json()["ttl_change"]["move"] == "90 -> 60"
    assert resp.json()["ttl_change"]["expires_rows"] is True
