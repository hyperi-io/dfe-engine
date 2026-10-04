#  Project:      dfe-engine
#  File:         tests/integration/test_query/test_views_api.py
#  Purpose:      POST /api/v1/queries/views/{label}/execute returns rows, as the restricted reader
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The view routes through the real API over a real ClickHouse.

The app starts with tenant isolation off, so the reader it runs views as exists
only because startup reconciles the service roles anyway. The first request builds
the executor from the password that reconcile stored; the view reports the user it
ran as, and a view over url() is refused by the reader's grants.
"""

import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from dfe_engine.api.app import create_app
from dfe_engine.api.deps import _registries, create_access_token
from dfe_engine.clickhouse.clickhouse_manager import ClickHouseManager
from dfe_engine.governance.ch.bootstrap import TENANT_ISOLATION_ENV
from dfe_engine.governance.ch.models import DEFAULT_SERVICE_ROLES
from dfe_engine.settings import (
    APISettings,
    AuthSettings,
    ClickHouseResilienceSettings,
    ClickHouseSettings,
    DFESettings,
    KafkaSettings,
    LocalAuthSettings,
    SecretsSettings,
)

pytestmark = pytest.mark.integration

ADMIN_PASSWORD = "test-admin-pw"
# The reconcile resolves the reader's grants against the data database the
# process-wide settings name, which is this default under the test environment.
DATA_DB = "dfe"
READER = "dfe_query_reader"


def _settings(tmp_path: Path, ch_params: dict) -> DFESettings:
    for sub in ("auth", "secrets"):
        (tmp_path / sub).mkdir()
    return DFESettings(
        config_dir=str(tmp_path),
        clickhouse=ClickHouseSettings(
            host=ch_params["host"],
            port=ch_params["port"],
            username=ch_params["username"],
            password=ch_params["password"],
            secure=False,
            data_database=DATA_DB,
            bootstrap_tables=False,
            resilience=ClickHouseResilienceSettings(budget_seconds=5.0),
        ),
        kafka=KafkaSettings(ensure_topics=False),
        auth=AuthSettings(
            enabled=True,
            auth_dir=str(tmp_path / "auth"),
            local=LocalAuthSettings(admin_password=ADMIN_PASSWORD),
        ),
        secrets=SecretsSettings(provider="file", path=str(tmp_path / "secrets")),
        api=APISettings(jwt_secret="test-secret-key-for-unit-tests-hmac32"),
    )


@pytest.fixture
def stack(ch_params, ch_client, tmp_path, monkeypatch):
    """The engine API with tenant isolation off, over a table and two parameterised views."""
    service_users = {r.user() for r in DEFAULT_SERVICE_ROLES}
    taken = {
        r[0]
        for r in ch_client.query("SELECT name FROM system.users").result_rows
        if r[0] in service_users
    }
    if taken:
        pytest.skip(f"refusing to reconcile: service users already exist here: {sorted(taken)}")

    monkeypatch.setenv(TENANT_ISOLATION_ENV, "false")
    for role in DEFAULT_SERVICE_ROLES:
        monkeypatch.delenv(f"DFE_CLICKHOUSE_{role.name.upper()}_PASSWORD", raising=False)
    monkeypatch.delenv("DFE_QUERY_VIEWS_RESTRICTED_PASSWORD", raising=False)

    uid = uuid.uuid4().hex[:8]
    table = f"`{DATA_DB}`.`probe_{uid}`"
    who_view = f"dfe_v_probe{uid}_who"
    leak_view = f"dfe_v_probe{uid}_leak"
    ch_client.command(f"CREATE DATABASE IF NOT EXISTS `{DATA_DB}`")
    ch_client.command(f"CREATE TABLE {table} (org String, n UInt64) ENGINE = MergeTree ORDER BY n")
    # The admin token below carries no org claim, so the API resolves it to `default`.
    ch_client.command(f"INSERT INTO {table} VALUES ('default', 1), ('default', 2), ('other', 3)")
    ch_client.command(
        f"CREATE VIEW `{DATA_DB}`.`{who_view}` AS SELECT currentUser() AS who, n "
        f"FROM {table} WHERE org = {{org_id:String}}"
    )
    ch_client.command(
        f"CREATE VIEW `{DATA_DB}`.`{leak_view}` AS SELECT c "
        "FROM url('http://127.0.0.1:9/leak.csv', 'CSV', 'c String') "
        "WHERE {org_id:String} != ''"
    )

    settings = _settings(tmp_path, ch_params)
    ClickHouseManager.reset_instance()
    app = create_app(settings=settings)
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            app.state.account_store.reset_password("admin", ADMIN_PASSWORD)
            token = create_access_token(data={"sub": "admin"}, settings=settings)
            yield SimpleNamespace(
                app=app,
                client=client,
                admin={"Authorization": f"Bearer {token}"},
                who=f"probe{uid}/who",
                leak=f"probe{uid}/leak",
            )
    finally:
        _registries.clear()
        ClickHouseManager.reset_instance()
        ch_client.command(f"DROP VIEW IF EXISTS `{DATA_DB}`.`{who_view}`")
        ch_client.command(f"DROP VIEW IF EXISTS `{DATA_DB}`.`{leak_view}`")
        ch_client.command(f"DROP TABLE IF EXISTS {table} SYNC")
        for role in DEFAULT_SERVICE_ROLES:
            ch_client.command(f"DROP USER IF EXISTS `{role.user()}`")
            ch_client.command(f"DROP ROLE IF EXISTS `{role.role()}`")
            ch_client.command(f"DROP SETTINGS PROFILE IF EXISTS `{role.profile()}`")


def _execute(stack, label: str):
    return stack.client.post(f"/api/v1/queries/views/{label}/execute", headers=stack.admin, json={})


def test_startup_reconciles_the_reader_with_tenant_isolation_off(stack, ch_client):
    users = {r[0] for r in ch_client.query("SELECT name FROM system.users").result_rows}
    assert READER in users
    tenant_roles = ch_client.query(
        "SELECT count() FROM system.roles WHERE name = 'dfe_tenant_role'"
    ).result_rows
    assert tenant_roles == [(0,)]


def test_a_view_returns_rows_and_runs_as_the_reader(stack):
    resp = _execute(stack, stack.who)

    assert resp.status_code == 200, resp.text
    rows = sorted(resp.json()["rows"], key=lambda row: row["n"])
    assert rows == [{"who": READER, "n": 1}, {"who": READER, "n": 2}]
    assert stack.app.state.view_executor is not None


def test_a_view_over_url_is_refused_by_the_readers_grants(stack):
    resp = _execute(stack, stack.leak)

    assert resp.status_code == 500, resp.text
    assert resp.json()["code"] == "query_error"
    assert "ACCESS_DENIED" in resp.json()["message"]
