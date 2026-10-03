#  Project:      dfe-engine
#  File:         tests/integration/test_sampler_org_scope.py
#  Purpose:      The sampler over a real ClickHouse returns a held caller only its orgs' rows
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The sampler route through the real API over a real ClickHouse.

The engine's ClickHouse user is targeted by no row policy, so the rows a caller
gets back are exactly the rows the sampler's own WHERE selects. One table holds
three orgs' rows; an org_viewer bound at system scope with two of those orgs gets
only theirs, an admin gets all three, and a table with no ``_org_id`` is refused.
"""

import json
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
    SourceSettings,
)

pytestmark = pytest.mark.integration

ADMIN_PASSWORD = "test-admin-pw"
VIEWER_ORGS = ["test_org", "test_org_2"]
EVERY_ORG = ["other_org", *VIEWER_ORGS]


def _settings(tmp_path: Path, ch_params: dict) -> DFESettings:
    for sub in ("auth", "secrets", "sources"):
        (tmp_path / sub).mkdir()
    return DFESettings(
        config_dir=str(tmp_path),
        source=SourceSettings(sources_dir=str(tmp_path / "sources")),
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
    """The engine API over the harness ClickHouse: three orgs' rows, an admin and a held viewer."""
    database = f"dfe_sampler_{uuid.uuid4().hex[:8]}"
    ch_client.command(f"CREATE DATABASE `{database}`")
    ch_client.command(
        f"CREATE TABLE `{database}`.events (_org_id LowCardinality(String), _json String, "
        "timestamp_load DateTime64(3)) ENGINE = MergeTree ORDER BY timestamp_load"
    )
    ch_client.command(
        f"CREATE TABLE `{database}`.untenanted (_json String, timestamp_load DateTime64(3)) "
        "ENGINE = MergeTree ORDER BY timestamp_load"
    )
    values = ", ".join(
        f"('{org}', '{json.dumps({'org': org, 'n': n})}', '2026-10-01 00:00:0{n}')"
        for org in EVERY_ORG
        for n in range(2)
    )
    ch_client.command(f"INSERT INTO `{database}`.events VALUES {values}")
    ch_client.command(f"INSERT INTO `{database}`.untenanted VALUES ('{{}}', now64(3))")
    settings = _settings(tmp_path, ch_params)
    ClickHouseManager.reset_instance()
    app = create_app(settings=settings)
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            app.state.account_store.reset_password("admin", ADMIN_PASSWORD)
            app.state.group_store.create("multi-viewers", roles=["org_viewer"], members=["viewer"])
            app.state.group_store.update("multi-viewers", org_ids=VIEWER_ORGS)
            app.state.account_store.create(
                "viewer", "viewer-password-2026", groups=["multi-viewers"]
            )
            yield SimpleNamespace(
                client=client,
                database=database,
                admin=_bearer("admin", settings),
                viewer=_bearer("viewer", settings),
            )
    finally:
        _registries.clear()
        ClickHouseManager.reset_instance()
        ch_client.command(f"DROP DATABASE IF EXISTS `{database}` SYNC")


def _sample(stack, headers: dict[str, str], table: str, **body):
    return stack.client.post(
        "/api/v1/sample",
        headers=headers,
        json={"mode": "recent", "table": f"`{stack.database}`.`{table}`", "limit": 100, **body},
    )


def _orgs(resp) -> list[str]:
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "completed", body
    return sorted({row["org"] for row in body["result"]["rows"]})


def test_a_viewer_held_to_two_orgs_gets_only_their_rows(stack):
    assert _orgs(_sample(stack, stack.viewer, "events")) == VIEWER_ORGS


@pytest.mark.parametrize("mode", ["recent", "random"])
def test_a_filter_cannot_reach_a_third_orgs_rows(stack, mode):
    resp = _sample(
        stack, stack.viewer, "events", mode=mode, filter="1 = 1 OR _org_id = 'other_org'"
    )

    assert _orgs(resp) == VIEWER_ORGS


def test_an_admin_gets_every_orgs_rows(stack):
    assert _orgs(_sample(stack, stack.admin, "events")) == EVERY_ORG


def test_a_table_without_org_id_is_refused_to_the_held_viewer(stack):
    resp = _sample(stack, stack.viewer, "untenanted")

    assert resp.status_code == 403, resp.text
    assert "has no _org_id column" in resp.json()["message"]


def test_a_table_that_does_not_exist_is_a_bad_request_for_the_held_viewer(stack):
    resp = _sample(stack, stack.viewer, "absent")

    assert resp.status_code == 400, resp.text
    assert resp.json()["code"] == "bad_sample_request"
