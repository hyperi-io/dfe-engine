#  Project:      dfe-engine
#  File:         tests/integration/test_service_roles_late_clickhouse.py
#  Purpose:      An engine that boots before ClickHouse still makes the hunt runner's user
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Boot the engine with no ClickHouse at its address, then start one there.

The startup reconcile is what creates ``dfe_hunt_runner``. When it runs against a
ClickHouse that is not up yet it fails, and before the retry nothing ran it again
short of a restart, so the hunt runner dialled a user that did not exist and
crash-looped. Here the engine is never restarted: the user has to appear on its
own once ClickHouse answers, with tenant isolation off (the service-role reconcile)
and on (the full one).
"""

import time
from pathlib import Path

import clickhouse_connect
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
HUNT_RUNNER = "dfe_hunt_runner"
# The retry's back-off doubles from 5s, so four retries fit in this with room to spare.
USER_DEADLINE_SECONDS = 120.0


def _settings(tmp_path: Path, params: dict) -> DFESettings:
    for sub in ("auth", "secrets"):
        (tmp_path / sub).mkdir()
    return DFESettings(
        config_dir=str(tmp_path),
        clickhouse=ClickHouseSettings(
            host=params["host"],
            port=params["port"],
            username=params["username"],
            password=params["password"],
            secure=False,
            bootstrap_tables=False,
            resilience=ClickHouseResilienceSettings(budget_seconds=2.0),
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


def _users(client) -> set[str]:
    return {row[0] for row in client.query("SELECT name FROM system.users").result_rows}


@pytest.fixture
def hunt_runner_minted(monkeypatch):
    """The seeded service roles with the hunt runner's user minted, whatever the pin ships.

    dfe-schemas declares the hunt runner a minted user from 9f39032 on; a pinned
    release that predates it would make no user for this test to wait for.
    """
    roles = [
        role.model_copy(update={"mint_user": True}) if role.name == "hunt_runner" else role
        for role in DEFAULT_SERVICE_ROLES
    ]
    monkeypatch.setattr("dfe_engine.governance.ch.reconciler.DEFAULT_SERVICE_ROLES", roles)


@pytest.mark.parametrize(
    ("isolation", "trigger"),
    [("false", "ch_service_role_reconcile"), ("true", "ch_rbac_reconcile")],
    ids=["isolation-off", "isolation-on"],
)
def test_the_hunt_runner_user_appears_once_a_late_clickhouse_answers(
    late_clickhouse, hunt_runner_minted, tmp_path, monkeypatch, isolation, trigger
):
    params, start_clickhouse = late_clickhouse
    monkeypatch.setenv(TENANT_ISOLATION_ENV, isolation)
    for role in DEFAULT_SERVICE_ROLES:
        monkeypatch.delenv(f"DFE_CLICKHOUSE_{role.name.upper()}_PASSWORD", raising=False)

    settings = _settings(tmp_path, params)
    ClickHouseManager.reset_instance()
    app = create_app(settings=settings)
    try:
        with TestClient(app, raise_server_exceptions=False) as api:
            # Nothing listens yet: the startup run failed and its retry is waiting.
            assert getattr(app.state, trigger).wait_idle(0.0) is False

            # The API is up for configuration work while ClickHouse is absent.
            app.state.account_store.reset_password("admin", ADMIN_PASSWORD)
            token = create_access_token(data={"sub": "admin"}, settings=settings)
            orgs = api.get("/api/v1/orgs", headers={"Authorization": f"Bearer {token}"})
            assert orgs.status_code == 200, orgs.text

            start_clickhouse()
            admin = clickhouse_connect.get_client(**params)
            deadline = time.monotonic() + USER_DEADLINE_SECONDS
            while HUNT_RUNNER not in _users(admin) and time.monotonic() < deadline:
                time.sleep(1.0)

            assert HUNT_RUNNER in _users(admin), (
                f"{HUNT_RUNNER} did not appear within {USER_DEADLINE_SECONDS:.0f}s of "
                "ClickHouse answering, without an engine restart"
            )
            assert getattr(app.state, trigger).wait_idle(30.0), "the retry kept running"
    finally:
        _registries.clear()
        ClickHouseManager.reset_instance()
