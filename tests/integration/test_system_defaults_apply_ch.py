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
"""

import shutil
import uuid
from pathlib import Path
from types import SimpleNamespace

import dfe_schemas
import pytest
from fastapi.testclient import TestClient

from dfe_engine.api.app import create_app
from dfe_engine.api.deps import _registries, create_access_token
from dfe_engine.clickhouse.clickhouse_manager import ClickHouseManager
from dfe_engine.gitcrud import GitCrud, default_registry
from dfe_engine.gitcrud.table_defaults import commit_patch
from dfe_engine.gitops.repo import GitopsRepo
from dfe_engine.schema.phase import apply_plan
from dfe_engine.schema.plan import build_plan
from dfe_engine.schema.schema_loader import SchemaLoader
from dfe_engine.settings import (
    APISettings,
    AuthSettings,
    ClickHouseResilienceSettings,
    ClickHouseSettings,
    DFESettings,
    HuntsSettings,
    KafkaSettings,
    LocalAuthSettings,
    SchemasSettings,
    SecretsSettings,
    ServicesSettings,
    SourceSettings,
)

pytestmark = pytest.mark.integration

DEFAULTS = "/api/v1/system/defaults"
APPLY = "/api/v1/system/defaults/apply"
DRIFT = "/api/v1/system/defaults/drift"
REBUILD = "needs a table rebuild; applies to new tables only"


def _settings(tmp_path: Path, ch_params: dict, database: str) -> DFESettings:
    for sub in ("sources", "services", "rules", "hunts", "auth", "secrets"):
        (tmp_path / sub).mkdir()
    # A copy of the shipped tree, so the build reads real definitions and nothing
    # this test does can write into the installed package.
    schemas_dir = tmp_path / "schemas"
    shutil.copytree(Path(dfe_schemas.__file__).parent / "data", schemas_dir)
    return DFESettings(
        config_dir=str(tmp_path),
        clickhouse=ClickHouseSettings(
            host=ch_params["host"],
            port=ch_params["port"],
            username=ch_params["username"],
            password=ch_params["password"],
            secure=False,
            data_database=database,
            default_ttl_days=90,
            bootstrap_tables=False,
            resilience=ClickHouseResilienceSettings(budget_seconds=5.0),
        ),
        kafka=KafkaSettings(ensure_topics=False),
        source=SourceSettings(sources_dir=str(tmp_path / "sources")),
        services=ServicesSettings(config_yaml_dir=str(tmp_path / "services")),
        schemas=SchemasSettings(schemas_dir=str(schemas_dir)),
        hunts=HuntsSettings(rules_dir=str(tmp_path / "rules"), hunt_dir=str(tmp_path / "hunts")),
        auth=AuthSettings(
            enabled=True,
            auth_dir=str(tmp_path / "auth"),
            local=LocalAuthSettings(admin_password="test-admin-pw"),
        ),
        secrets=SecretsSettings(provider="file", path=str(tmp_path / "secrets")),
        api=APISettings(jwt_secret="test-secret-key-for-unit-tests-hmac32"),
    )


@pytest.fixture
def stack(ch_params, ch_client, monkeypatch, tmp_path):
    """The engine API over a real ClickHouse carrying the core tables at a 90-day default."""
    database = f"dfe_defaults_{uuid.uuid4().hex[:8]}"
    settings = _settings(tmp_path, ch_params, database)
    # The build and plan routes read the process settings rather than the app's.
    monkeypatch.setattr("dfe_engine.settings._settings", settings)
    ClickHouseManager.reset_instance()
    report = apply_plan(ch_client, build_plan(settings=settings, client=ch_client))
    assert not report.refused, [outcome.describe() for outcome in report.refused]
    app = create_app(settings=settings)
    token = create_access_token(
        data={"sub": "admin", "org_id": "test-org", "roles": ["admin"]}, settings=settings
    )
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            # Off the issued password, which refuses every other call until changed.
            app.state.account_store.reset_password("admin", "test-admin-pw")
            gc = GitCrud(
                GitopsRepo(local_path=str(tmp_path / "deploy"), push=False), default_registry()
            )
            app.state.gitcrud = gc
            yield SimpleNamespace(
                client=client,
                crud=gc,
                database=database,
                headers={"Authorization": f"Bearer {token}"},
            )
    finally:
        _registries.clear()
        ClickHouseManager.reset_instance()
        ch_client.command(f"DROP DATABASE IF EXISTS `{database}` SYNC")


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


def _deploy_source(
    stack, name: str, *, header: dict | None = None, schema: dict | None = None
) -> None:
    created = stack.client.post(
        "/api/v1/sources",
        headers=stack.headers,
        json={
            "source": name,
            "display_name": name,
            "enabled": True,
            "match": {"field": "tags.collector.type", "value": name},
            "header": header or {"type": "timeseries", "version": "1.0.0"},
            "schema": schema or {"meta_schema": "meta/syslog"},
        },
    )
    assert created.status_code in (200, 201), created.text
    deployed = stack.client.post(f"/api/v1/sources/{name}/deploy", headers=stack.headers)
    assert deployed.status_code == 200, deployed.text


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


def test_apply_moves_a_deployed_tables_ttl_without_a_restart(ch_client, stack):
    db = stack.database
    _deploy_source(stack, "syslog-ttl", schema={"meta_schema": "meta/syslog", "ttl_days": 90})
    # The default reconcile leaves a table whose source pins its own TTL alone.
    _patch(stack, {"ttl_days": 91})
    assert _live_ttl_days(ch_client, db, "syslog-ttl") == 90

    body = _apply(stack, "syslog-ttl")

    assert _live_ttl_days(ch_client, db, "syslog-ttl") == 91
    assert body["updated"] == ["syslog-ttl"]
    assert body["live"] == [
        {
            "source": "syslog-ttl",
            "status": "altered",
            "table": f"{db}.syslog-ttl",
            "ttl": "90 -> 91",
            "columns_added": [],
            "not_applied": [],
            "reason": None,
        }
    ]
    assert "syslog-ttl" not in _drift(stack)


def test_drift_reads_the_ttl_the_table_runs_not_the_one_the_source_stores(ch_client, stack):
    db = stack.database
    _patch(stack, {"ttl_days": 91})
    _deploy_source(stack, "syslog-moved", schema={"meta_schema": "meta/syslog", "ttl_days": 91})
    ch_client.command(
        f"ALTER TABLE `{db}`.`syslog-moved` MODIFY TTL _timestamp_load + INTERVAL 90 DAY"
    )

    drift = _drift(stack)["syslog-moved"]

    assert drift["drifted"] == ["ttl_days"]
    assert drift["ttl_days"] == {"stored": 91, "default": 91, "live": 90}


def test_apply_adds_the_pinned_headers_columns_to_a_deployed_table(ch_client, stack):
    db = stack.database
    _deploy_source(stack, "syslog-minimal", header={"type": "minimal", "version": "1.0.0"})
    before = _columns(ch_client, db, "syslog-minimal")
    header = {col.name for col in SchemaLoader.load_profile("timeseries", profile_version="1.0.0")}
    missing = header - set(before)
    assert {"_timestamp_received", "_tags"} <= missing

    body = _apply(stack, "syslog-minimal")

    assert header <= set(_columns(ch_client, db, "syslog-minimal"))
    live = body["live"][0]
    assert live["status"] == "altered"
    assert set(live["columns_added"]) == missing
    assert live["not_applied"] == []
    assert "syslog-minimal" not in _drift(stack)


def test_an_engine_change_needs_a_rebuild_and_stays_drift(ch_client, stack):
    db = stack.database
    _deploy_source(stack, "syslog-engine")
    _patch(stack, {"engine": "ReplacingMergeTree"})

    body = _apply(stack, "syslog-engine")

    engine, _ = _table(ch_client, db, "syslog-engine")
    assert engine == "MergeTree"
    drift = _drift(stack)["syslog-engine"]
    assert drift["drifted"] == ["engine"]
    assert drift["engine"] == {
        "stored": "ReplacingMergeTree",
        "default": "ReplacingMergeTree",
        "live": "MergeTree",
    }
    assert drift["ttl_days"]["live"] == 90
    assert body["updated"] == ["syslog-engine"]
    live = body["live"][0]
    assert live["status"] == "unchanged"
    assert live["not_applied"] == [{"field": "engine", "reason": REBUILD}]


def test_an_existing_header_column_keeps_its_nullability(ch_client, stack):
    db = stack.database
    _deploy_source(stack, "syslog-nullable")
    # The header declares _uuid Nullable(UUID); the table now holds it non-nullable.
    ch_client.command(f"ALTER TABLE `{db}`.`syslog-nullable` MODIFY COLUMN `_uuid` UUID")
    retyped = {"_uuid"}
    before = _columns(ch_client, db, "syslog-nullable")

    body = _apply(stack, "syslog-nullable")

    assert _columns(ch_client, db, "syslog-nullable")["_uuid"] == before["_uuid"]
    live = body["live"][0]
    assert live["status"] == "unchanged"
    [kept] = live["not_applied"]
    assert kept["field"] == "common_header_version"
    _, _, named = kept["reason"].partition(": ")
    assert set(named.split(", ")) == retyped


def test_plan_and_dry_run_show_the_ttl_move_and_that_it_deletes_rows(ch_client, stack):
    db = stack.database
    _deploy_source(stack, "syslog-plan")
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
    _deploy_source(
        stack, "syslog-path", header={"type": "common-header/timeseries", "version": "1.0.0"}
    )

    assert "syslog-path" not in _drift(stack)


def test_one_table_clickhouse_refuses_does_not_hide_the_others(ch_client, stack):
    db = stack.database
    _deploy_source(stack, "syslog-good", schema={"meta_schema": "meta/syslog", "ttl_days": 90})
    _deploy_source(stack, "syslog-bad", schema={"meta_schema": "meta/syslog", "ttl_days": 90})
    # A view under the deployed table's name: every ALTER on it is refused.
    ch_client.command(f"DROP TABLE `{db}`.`syslog-bad` SYNC")
    ch_client.command(f"CREATE VIEW `{db}`.`syslog-bad` AS SELECT 1 AS x")
    _patch(stack, {"ttl_days": 91})

    body = _apply(stack, "syslog-bad", "syslog-good")

    assert _live_ttl_days(ch_client, db, "syslog-good") == 91
    assert body["updated"] == ["syslog-bad", "syslog-good"]
    bad, good = body["live"]
    assert bad["source"] == "syslog-bad"
    assert bad["status"] == "failed"
    assert "ClickHouse rejected" in bad["reason"]
    assert good["source"] == "syslog-good"
    assert good["status"] == "altered"
    assert good["ttl"] == "90 -> 91"


def test_a_redeploy_moves_an_existing_tables_ttl(ch_client, stack):
    db = stack.database
    _deploy_source(stack, "syslog-redeploy")
    assert _live_ttl_days(ch_client, db, "syslog-redeploy") == 90
    # Stored without the reconcile a PATCH runs, so only the deploy can move the table.
    commit_patch(stack.crud, actor="test", ttl_days=60)

    resp = stack.client.post("/api/v1/sources/syslog-redeploy/deploy", headers=stack.headers)

    assert resp.status_code == 200, resp.text
    assert _live_ttl_days(ch_client, db, "syslog-redeploy") == 60
    assert resp.json()["statements_applied"] >= 1
    assert resp.json()["ttl_change"]["move"] == "90 -> 60"
    assert resp.json()["ttl_change"]["expires_rows"] is True
