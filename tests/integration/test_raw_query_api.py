#  Project:      dfe-engine
#  File:         tests/integration/test_raw_query_api.py
#  Purpose:      POST /api/v1/queries/raw against a real ClickHouse: admin-only, read-only
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The raw query route through the real API over a real ClickHouse.

The route runs caller SQL as the engine's own ClickHouse user, which may write.
A viewer is refused before any SQL is sent; an admin reads, and ClickHouse refuses
the admin's writes, table-function writes included, because the adapter sends
``readonly=1``.
"""

import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from dfe_engine.api.app import create_app
from dfe_engine.api.deps import _registries, create_access_token
from dfe_engine.clickhouse.clickhouse_manager import ClickHouseManager
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

RAW = "/api/v1/queries/raw"
ADMIN_PASSWORD = "test-admin-pw"


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


def _bearer(sub: str, settings: DFESettings) -> dict[str, str]:
    token = create_access_token(data={"sub": sub}, settings=settings)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def stack(ch_params, ch_client, tmp_path):
    """The engine API over the harness ClickHouse, with an admin and a data viewer."""
    database = f"dfe_raw_{uuid.uuid4().hex[:8]}"
    ch_client.command(f"CREATE DATABASE `{database}`")
    ch_client.command(
        f"CREATE TABLE `{database}`.events (id UInt64) ENGINE = MergeTree ORDER BY id"
    )
    ch_client.command(f"INSERT INTO `{database}`.events VALUES (1), (2)")
    settings = _settings(tmp_path, ch_params)
    ClickHouseManager.reset_instance()
    app = create_app(settings=settings)
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            # Off the issued password, which refuses every other call until changed.
            app.state.account_store.reset_password("admin", ADMIN_PASSWORD)
            app.state.group_store.create("raw-viewers", roles=["data_viewer"], members=["reader"])
            app.state.account_store.create("reader", "reader-password-2026", groups=["raw-viewers"])
            yield SimpleNamespace(
                client=client,
                database=database,
                admin=_bearer("admin", settings),
                viewer=_bearer("reader", settings),
            )
    finally:
        _registries.clear()
        ClickHouseManager.reset_instance()
        ch_client.command(f"DROP DATABASE IF EXISTS `{database}` SYNC")


def _raw(stack, headers: dict[str, str], sql: str):
    return stack.client.post(
        RAW, headers=headers, json={"datasource": "clickhouse:default", "query": sql}
    )


def test_a_data_viewer_is_refused_before_any_sql_runs(stack):
    resp = _raw(stack, stack.viewer, "SELECT currentUser() AS u")

    assert resp.status_code == 403, resp.text


def test_an_admin_reads(stack):
    resp = _raw(stack, stack.admin, f"SELECT count() AS n FROM `{stack.database}`.events")

    assert resp.status_code == 200, resp.text
    assert resp.json()["rows"] == [{"n": 2}]


@pytest.mark.parametrize(
    "statement",
    [
        "CREATE TABLE `{db}`.made (x UInt8) ENGINE = Log",
        "INSERT INTO `{db}`.events VALUES (3)",
        "DROP TABLE `{db}`.events",
        "ALTER TABLE `{db}`.events DELETE WHERE 1",
        "INSERT INTO FUNCTION url('http://127.0.0.1:8123/?query=INSERT%20INTO%20{db}.events"
        "%20FORMAT%20TSV', 'TSV', 'id UInt64') VALUES (3)",
    ],
    ids=["create", "insert", "drop", "alter", "insert-into-url"],
)
def test_an_admin_write_is_refused_by_clickhouse(stack, ch_client, statement):
    resp = _raw(stack, stack.admin, statement.format(db=stack.database))

    assert resp.status_code == 500, resp.text
    assert resp.json()["code"] == "query_error"
    assert "readonly" in resp.json()["message"].lower()
    rows = ch_client.query(f"SELECT count() FROM `{stack.database}`.events").result_rows
    assert rows == [(2,)]
