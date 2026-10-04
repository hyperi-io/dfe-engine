#  Project:      dfe-engine
#  File:         tests/integration/test_hunt_runner_connect.py
#  Purpose:      The hunt runner backs off on a refused login until its user exists
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The runner's startup connect, against a ClickHouse that does not know its user yet.

The engine creates the runner's user in a reconcile that may land after the runner
starts. The runner used to exit on the refused login and crash-loop its pod; here
it has to back off, and connect on its own once the user is made.
"""

import uuid

import clickhouse_connect
import pytest
from prometheus_client.parser import text_string_to_metric_families
from scalo.metrics import create_metrics

from dfe_engine.hunt_runner.cli import _connect_coordinator
from dfe_engine.hunt_runner.connect import connect_when_available
from dfe_engine.hunt_runner.metrics import CLICKHOUSE_UNAVAILABLE, HuntRunnerMetrics
from dfe_engine.settings import (
    APISettings,
    ClickHouseResilienceSettings,
    ClickHouseSettings,
    DFESettings,
)

pytestmark = pytest.mark.integration

PASSWORD = "runner-probe-password"
# Sleeps the runner takes before the user is made; each one is a refused login.
REFUSALS_BEFORE_THE_USER_EXISTS = 3


def _unavailable(manager) -> dict[tuple[str, str], float]:
    counts: dict[tuple[str, str], float] = {}
    for family in text_string_to_metric_families(manager.metrics_text):
        for sample in family.samples:
            if sample.name == CLICKHOUSE_UNAVAILABLE:
                counts[(sample.labels["stage"], sample.labels["reason"])] = sample.value
    return counts


def test_a_refused_login_backs_off_and_connects_once_the_user_exists(ch_params, dfe_db):
    user = f"dfe_runner_probe_{uuid.uuid4().hex[:8]}"
    admin = clickhouse_connect.get_client(**ch_params)
    settings = DFESettings(
        clickhouse=ClickHouseSettings(
            host=ch_params["host"],
            port=ch_params["port"],
            username=user,
            password=PASSWORD,
            secure=False,
            data_database=dfe_db,
        ),
        api=APISettings(jwt_secret="test-secret-key-for-unit-tests-hmac32"),
    )
    manager = create_metrics("test", backend="prometheus", enable_auto_update=False)
    waits: list[float] = []

    def make_the_user_on_the_third_wait(seconds: float) -> None:
        """The engine's reconcile landing while the runner waits."""
        waits.append(seconds)
        if len(waits) == REFUSALS_BEFORE_THE_USER_EXISTS:
            admin.command(f"CREATE USER `{user}` IDENTIFIED WITH sha256_password BY '{PASSWORD}'")
            admin.command(f"GRANT SELECT, INSERT ON `{dfe_db}`.* TO `{user}`")

    try:
        client, coord = connect_when_available(
            lambda: _connect_coordinator(settings),
            resilience=ClickHouseResilienceSettings(wait_initial=0.05, wait_max=0.2),
            metrics=HuntRunnerMetrics(manager),
            user=user,
            sleep=make_the_user_on_the_third_wait,
        )
        try:
            assert client.query("SELECT currentUser()").result_rows == [(user,)]
            assert coord.active_count(0) == 0
        finally:
            client.close()
    finally:
        admin.command(f"DROP USER IF EXISTS `{user}`")

    assert waits == [0.05, 0.1, 0.2]
    assert _unavailable(manager) == {("connect", "authentication"): 3.0}
