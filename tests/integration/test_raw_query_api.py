#  Project:      dfe-engine
#  File:         tests/integration/test_raw_query_api.py
#  Purpose:      POST /api/v1/queries/raw against a real ClickHouse: admin-only, read-only
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The raw query route through the real API over a real ClickHouse.

The route runs caller SQL as the engine's own ClickHouse user, which may write.
A viewer is refused before any SQL is sent. An admin reads, and ClickHouse refuses
the admin's writes, table-function writes included, because the adapter sends
``readonly=1``. The refusal's text reaches the log, under the reference the answer names.
"""

import asyncio
import re
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from scalo.logger import logger

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
from tests.support.accounts import admin_on_its_own_password
from tests.support.clickhouse_cleanup import drop_database_and_policies

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
            admin_on_its_own_password(app)
            app.state.group_store.create("raw-viewers", roles=["data_viewer"], members=["reader"])
            app.state.account_store.create("reader", "reader-password-2026", groups=["raw-viewers"])
            yield SimpleNamespace(
                app=app,
                client=client,
                database=database,
                admin=_bearer("admin", settings),
                viewer=_bearer("reader", settings),
            )
    finally:
        _registries.clear()
        ClickHouseManager.reset_instance()
        drop_database_and_policies(ch_client, database)


def _raw(stack, headers: dict[str, str], sql: str, **options: int):
    body: dict = {"datasource": "clickhouse:default", "query": sql}
    if options:
        body["options"] = options
    return stack.client.post(RAW, headers=headers, json=body)


def test_a_slow_query_does_not_hold_the_event_loop(stack):
    """A liveness probe sent while the query sleeps answers before the query does."""
    finished: list[str] = []

    async def race() -> None:
        transport = httpx.ASGITransport(app=stack.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://engine") as client:

            async def slow() -> None:
                resp = await client.post(
                    RAW,
                    headers=stack.admin,
                    json={"datasource": "clickhouse:default", "query": "SELECT sleep(2)"},
                )
                assert resp.status_code == 200, resp.text
                finished.append("query")

            async def probe() -> None:
                await asyncio.sleep(0.2)
                resp = await client.get("/livez")
                assert resp.status_code == 200, resp.text
                finished.append("livez")

            await asyncio.gather(slow(), probe())

    asyncio.run(race())

    assert finished == ["livez", "query"]


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
    captured: list = []
    handler_id = logger.add(captured.append, level="WARNING")
    try:
        resp = _raw(stack, stack.admin, statement.format(db=stack.database))
    finally:
        logger.remove(handler_id)

    assert resp.status_code == 500, resp.text
    assert resp.json()["code"] == "query_error"
    # ClickHouse's reason is in the log line the answer's reference names, never the answer.
    message = resp.json()["message"]
    assert "readonly" not in message.lower()
    reference = re.search(r"\(reference ([0-9a-f]{12})\)$", message)
    assert reference is not None, message
    logged = [
        m.record["extra"]["error"]
        for m in captured
        if m.record["extra"].get("reference") == reference.group(1)
    ]
    assert len(logged) == 1
    assert "readonly" in logged[0].lower()
    rows = ch_client.query(f"SELECT count() FROM `{stack.database}`.events").result_rows
    assert rows == [(2,)]
