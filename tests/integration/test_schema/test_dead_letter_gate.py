#  Project:      dfe-engine
#  File:         tests/integration/test_schema/test_dead_letter_gate.py
#  Purpose:      A deployment that cannot record a dead letter is held NotReady
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The whole boot phase against a real ClickHouse and a broker that is not there.

The phase is what readiness reads, and Compose gates every data-plane container
on that, so a failed pass is the deployment refusing to start.
"""

import uuid

import pytest

from dfe_engine.clickhouse.clickhouse_manager import ClickHouseManager
from dfe_engine.schema import phase
from dfe_engine.settings import DFESettings

pytestmark = pytest.mark.integration

# Nothing listens on port 1, so every admin call fails as an absent broker does.
ABSENT_BROKER = "127.0.0.1:1"


@pytest.fixture
def local_ch(ch_params):
    if ch_params["host"] != "127.0.0.1":
        pytest.skip("applies the whole manifest, so it runs only on a throwaway local ClickHouse")
    return ch_params


@pytest.fixture(autouse=True)
def _fresh_phase():
    ClickHouseManager.reset_instance()
    yield
    ClickHouseManager.reset_instance()
    phase._set_state(phase.SchemaBootstrapState())


def _settings(ch: dict, *, bus: bool) -> DFESettings:
    return DFESettings(
        env="dev",
        clickhouse={
            "host": ch["host"],
            "port": ch["port"],
            "username": ch["username"],
            "password": ch["password"],
            "secure": False,
            "data_database": f"dfe_dlqgate_{uuid.uuid4().hex[:8]}",
            "bootstrap_tables": True,
            "bootstrap_wait_seconds": 0,
        },
        transport={"default": "bus" if bus else "direct", "bus_present": bus},
        kafka={"bootstrap_servers": ABSENT_BROKER},
    )


def test_a_broker_that_cannot_hold_the_dead_letters_leaves_the_engine_not_ready(local_ch):
    state = phase.run_bootstrap(settings=_settings(local_ch, bus=True))

    assert state.state == phase.STATE_FAILED
    assert "dead-letter topic(s) are not on the broker" in state.error
    assert "dfe_receiver_dlq" in state.error
    assert "dfe_loader_dlq" in state.error
    assert phase.schema_ready() is False


def test_a_deployment_with_no_bus_still_converges(local_ch):
    state = phase.run_bootstrap(settings=_settings(local_ch, bus=False))

    assert state.state == phase.STATE_CONVERGED, state.error
    assert phase.schema_ready() is True
