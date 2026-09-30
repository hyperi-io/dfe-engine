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
through the API, and every assertion reads ``system.tables``.
"""

import re
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
from dfe_engine.gitcrud.retention import effective_settings
from dfe_engine.gitops.repo import GitopsRepo
from dfe_engine.schema.phase import apply_plan
from dfe_engine.schema.plan import build_plan
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

URL = "/api/v1/system/retention"
_TTL_DAYS_RE = re.compile(r"toIntervalDay\((\d+)\)")


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
    database = f"dfe_retention_{uuid.uuid4().hex[:8]}"
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


def _live_ttl_days(ch_client, database: str, table: str) -> int | None:
    rows = ch_client.query(
        "SELECT engine_full FROM system.tables WHERE database = {db:String} AND name = {t:String}",
        parameters={"db": database, "t": table},
    ).result_rows
    assert rows, f"{database}.{table} does not exist"
    _, sep, clause = str(rows[0][0]).partition(" TTL ")
    if not sep:
        return None
    match = _TTL_DAYS_RE.search(clause)
    return int(match.group(1)) if match else None


def _deploy_source(stack, name: str) -> None:
    created = stack.client.post(
        "/api/v1/sources",
        headers=stack.headers,
        json={
            "source": name,
            "display_name": name,
            "enabled": True,
            "match": {"field": "tags.collector.type", "value": name},
            "header": {"type": "timeseries", "version": "1.0.0"},
            "schema": {"meta_schema": "meta/syslog"},
        },
    )
    assert created.status_code in (200, 201), created.text
    deployed = stack.client.post(f"/api/v1/sources/{name}/deploy", headers=stack.headers)
    assert deployed.status_code == 200, deployed.text


def test_an_admin_change_moves_every_table_that_follows_the_default(ch_client, stack):
    _deploy_source(stack, "syslog-before")
    db = stack.database
    assert _live_ttl_days(ch_client, db, "main") == 90
    assert _live_ttl_days(ch_client, db, "syslog-before") == 90
    assert _live_ttl_days(ch_client, db, "detection_checkpoint") == 30

    resp = stack.client.put(URL, headers=stack.headers, json={"default_ttl_days": 45})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["default_ttl_days"] == 45
    assert body["stored"] == 45
    assert body["origin"] == "override"
    assert body["deployment_default"] == 90
    assert f"{db}.main: 90 -> 45" in body["reconcile"]["core_tables_altered"]
    assert f"{db}.syslog-before" in body["reconcile"]["source_tables_altered"]
    assert body["reconcile"]["sources_reconciled"] == 1
    assert body["reconcile"]["sources_skipped"] == 0

    for table in ("main", "detection", "otel_logs", "otel_traces", "syslog-before"):
        assert _live_ttl_days(ch_client, db, table) == 45, table
    # Declares its own TTL in dfe-schemas, so the default never governs it.
    assert _live_ttl_days(ch_client, db, "detection_checkpoint") == 30

    assert stack.crud.get("gov_settings", "retention")["default_ttl_days"] == 45
    assert stack.client.get(URL, headers=stack.headers).json()["default_ttl_days"] == 45


def test_a_source_built_or_deployed_after_the_change_takes_it(ch_client, stack):
    assert (
        stack.client.put(URL, headers=stack.headers, json={"default_ttl_days": 45}).status_code
        == 200
    )

    _deploy_source(stack, "syslog-after")
    built = stack.client.post("/api/v1/sources/syslog-after/build", headers=stack.headers)

    assert _live_ttl_days(ch_client, stack.database, "syslog-after") == 45
    assert built.status_code == 200, built.text
    assert "INTERVAL 45 DAY" in built.json()["ddl"]["create_table"]


def test_zero_removes_the_ttl_and_clearing_restores_the_deployment_default(ch_client, stack):
    _deploy_source(stack, "syslog-zero")
    db = stack.database
    # The stored build a console's plan step leaves behind carries no columns, and the
    # TTL has to be placed over one; the pass must not lean on it.
    stored = stack.client.post("/api/v1/sources/syslog-zero/build", headers=stack.headers)
    assert stored.status_code == 200, stored.text

    zero = stack.client.put(URL, headers=stack.headers, json={"default_ttl_days": 0})

    assert zero.status_code == 200, zero.text
    assert f"{db}.main: 90 -> none" in zero.json()["reconcile"]["core_tables_altered"]
    assert _live_ttl_days(ch_client, db, "main") is None
    assert _live_ttl_days(ch_client, db, "syslog-zero") is None
    assert _live_ttl_days(ch_client, db, "detection_checkpoint") == 30

    cleared = stack.client.put(URL, headers=stack.headers, json={"default_ttl_days": None})

    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["origin"] == "deployment"
    assert cleared.json()["stored"] is None
    assert cleared.json()["default_ttl_days"] == 90
    assert _live_ttl_days(ch_client, db, "main") == 90
    assert f"{db}.syslog-zero" in cleared.json()["reconcile"]["source_tables_altered"]
    assert _live_ttl_days(ch_client, db, "syslog-zero") == 90


def test_the_next_boot_renders_the_override_so_nothing_reads_as_drift(ch_client, stack):
    put = stack.client.put(URL, headers=stack.headers, json={"default_ttl_days": 45})
    assert put.status_code == 200, put.text

    settings = effective_settings(stack.client.app.state.settings, stack.crud)
    report = apply_plan(ch_client, build_plan(settings=settings, client=ch_client))

    assert settings.clickhouse.default_ttl_days == 45
    assert not report.refused, [outcome.describe() for outcome in report.refused]
    assert _live_ttl_days(ch_client, stack.database, "main") == 45
