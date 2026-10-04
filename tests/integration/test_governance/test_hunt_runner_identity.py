#  Project:      dfe-engine
#  File:         tests/integration/test_governance/test_hunt_runner_identity.py
#  Purpose:      The hunt runner's own ClickHouse user runs a hunt and reaches nothing else
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""``dfe_hunt_runner`` runs a real hunt as itself, and is refused everything else.

The worker runs ``INSERT ... SELECT`` built from rule text, so the identity it runs
as decides what a crafted rule can reach. The service-only reconcile mints the user
on the password a deployment provides, exactly as startup does with tenant isolation
off; the runner then ticks over a rule compiled from real files, connected as that
user, and the table functions and DDL a crafted rule would reach for are refused.

The catalogue flag comes from the dfe-schemas wheel, so it is set here the way the
role catalogue's 1.1.0 sets it. No mocks: a real ClickHouse and a real file store.
"""

import time
import uuid
from types import SimpleNamespace

import clickhouse_connect
import pytest
from clickhouse_connect.driver.exceptions import DatabaseError
from common.hunt_files import write_hunt, write_rule

from dfe_engine.governance.ch.models import DEFAULT_SERVICE_ROLES
from dfe_engine.governance.ch.reconciler import ChRbacReconciler
from dfe_engine.hunt_runner import ChCoordinator, HuntRunner, HuntWorker, load_specs
from dfe_engine.hunt_runner.spread import current_fire
from dfe_engine.secrets import build_secrets
from dfe_engine.settings import SecretsSettings

from .conftest import drop_safely

pytestmark = pytest.mark.integration

_RUNNER_USER = "dfe_hunt_runner"
_PROVIDED = "Provided4By7The2Deploy9Layer"  # gitleaks:allow - throwaway test password
# Unroutable by design: a request that leaves ClickHouse fails to connect, which is
# a different error from the grant refusal these assertions look for.
_URL = "url('http://127.0.0.1:9/leak.csv', 'CSV', 'c String')"
_S3 = "s3('http://127.0.0.1:9/bucket/key.csv', 'CSV', 'c String')"


def _service_users() -> list[str]:
    return [r.user() for r in DEFAULT_SERVICE_ROLES if r.mint_user or r.name == "hunt_runner"]


@pytest.fixture
def runner_world(ch_client, ch_params, dfe_db, tmp_path):
    """The service users reconciled into ``dfe_db``, and a client connected as the runner."""
    taken = {
        r[0]
        for r in ch_client.query("SELECT name FROM system.users").result_rows
        if r[0] in _service_users()
    }
    if taken:
        pytest.skip(f"refusing to reconcile: service users already exist here: {sorted(taken)}")

    roles = [
        r.model_copy(update={"mint_user": True}) if r.name == "hunt_runner" else r
        for r in DEFAULT_SERVICE_ROLES
    ]
    store = build_secrets(SecretsSettings(provider="file", path=str(tmp_path / "secrets")))
    runner = None
    try:
        result = ChRbacReconciler(
            ch_client,
            secrets_store=store,
            database=dfe_db,
            provided_passwords={"hunt_runner": _PROVIDED},
        ).reconcile_service_roles(roles)
        runner = clickhouse_connect.get_client(
            host=ch_params["host"],
            port=ch_params["port"],
            username=_RUNNER_USER,
            password=_PROVIDED,
            secure=ch_params["secure"],
        )
        yield SimpleNamespace(result=result, store=store, runner=runner, db=dfe_db)
    finally:
        if runner is not None:
            runner.close()
        for role in roles:
            drop_safely(ch_client, f"DROP USER IF EXISTS `{role.user()}`")
            drop_safely(ch_client, f"DROP ROLE IF EXISTS `{role.role()}`")
            drop_safely(ch_client, f"DROP SETTINGS PROFILE IF EXISTS `{role.profile()}`")


def _refusal(client, sql: str) -> str:
    """The error ClickHouse answers ``sql`` with; fails the test if it runs."""
    with pytest.raises(DatabaseError) as caught:
        client.query(sql)
    return str(caught.value)


def test_the_runner_user_carries_the_provided_password(runner_world):
    assert runner_world.result.errors == []
    assert "service/hunt_runner" in runner_world.result.minted
    assert runner_world.store.get("ch/service/hunt_runner") == _PROVIDED
    who = runner_world.runner.query("SELECT currentUser()").result_rows[0][0]
    assert who == _RUNNER_USER


def test_a_hunt_runs_as_the_runner_user(runner_world, ch_client, tmp_path):
    db = runner_world.db
    runner_client = runner_world.runner
    hunt = f"ident_{uuid.uuid4().hex[:8]}"
    rule_id = f"{hunt}_rule"
    org = f"org-{uuid.uuid4().hex[:8]}"
    write_rule(tmp_path / "rules", rule_id, f"_org_id = '{org}'")
    write_hunt(tmp_path / "hunts", hunt, rule_id, db)
    specs = load_specs(tmp_path / "hunts", rules_dir=tmp_path / "rules")

    fire = current_fire(hunt, 60, int(time.time()))
    now = fire + 1
    ch_client.command(
        f"INSERT INTO `{db}`.`main` (_timestamp_load, _timestamp, _org_id) "
        f"SELECT toDateTime64({fire} - 1 - number, 3), toDateTime64({fire} - 1 - number, 3), "
        f"'{org}' FROM numbers(3)"
    )
    coord = ChCoordinator(
        runner_client, database=db, worker_id="ident", settle_seconds=0.0, sleep=lambda _s: None
    )
    coord.ensure_schema()
    coord.heartbeat(now, 15.0)

    assert HuntRunner(coord, HuntWorker(runner_client, coord), specs, cap=2).tick(now) == 1
    detections = runner_client.query(
        f"SELECT count() FROM `{db}`.detection WHERE hunt_name = {{h:String}}",
        parameters={"h": hunt},
    ).result_rows[0][0]
    assert detections == 3


@pytest.mark.parametrize(
    "sql",
    [
        f"SELECT * FROM {_URL}",
        f"SELECT * FROM {_S3}",
        "SELECT * FROM remote('127.0.0.1:9', system.one)",
        "SELECT * FROM file('leak.csv', 'CSV', 'c String')",
        "SELECT name FROM system.users",
    ],
    ids=["url", "s3", "remote", "file", "system-users"],
)
def test_a_crafted_read_is_refused_by_grant(runner_world, sql):
    message = _refusal(runner_world.runner, sql)
    assert "ACCESS_DENIED" in message, message


def test_an_insert_select_from_url_is_refused_by_grant(runner_world):
    db = runner_world.db
    with pytest.raises(DatabaseError, match="ACCESS_DENIED"):
        runner_world.runner.command(
            f"INSERT INTO `{db}`.detection (hunt_name) SELECT c FROM {_URL}"
        )


def test_ddl_is_refused_by_grant(runner_world):
    db = runner_world.db
    with pytest.raises(DatabaseError, match="ACCESS_DENIED"):
        runner_world.runner.command(f"CREATE TABLE `{db}`.made (x UInt8) ENGINE = Memory")


def test_the_admin_reaches_url_so_the_refusal_is_the_grant(runner_world, ch_client):
    """The control: the same url() as the admin gets past the grant and fails on the network."""
    message = _refusal(ch_client, f"SELECT * FROM {_URL} SETTINGS max_execution_time = 5")
    assert "ACCESS_DENIED" not in message, message
