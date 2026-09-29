"""Shared fixtures for hunt integration tests.

Every ClickHouse here comes from the tiered harness in ``tests/integration/conftest.py``
(``ch_params``), never from ``get_settings()``, so a developer's local ClickHouse on
``localhost:8123`` is never dialled by accident.
"""

import uuid

import pytest

from dfe_engine.clickhouse.clickhouse_manager import ClickHouseManager


@pytest.fixture
def manager_client(ch_params):
    """The engine's own ClickHouseManager client wrapper, on the harness's ClickHouse.

    The code under test calls ``execute``, which only the wrapper provides, so the raw
    ``ch_client`` the harness hands out is not a substitute.
    """
    manager = ClickHouseManager(
        target_config_data={
            "ch_host": ch_params["host"],
            "ch_port": ch_params["port"],
            "ch_username": ch_params["username"],
            "ch_password": ch_params["password"],
            "ch_secure": ch_params["secure"],
        }
    )
    try:
        yield manager.get_clickhouse_client()
    finally:
        manager._cleanup()  # no public close: reset_instance only serves the singleton


@pytest.fixture
def unique_names():
    """A unique (database, table) pair per test so runs never collide and each
    test creates + drops its own throwaway audit table on the shared cluster."""
    unique_id = uuid.uuid4().hex
    return f"dfe_audit_{unique_id}", f"detection_checkpoint_{unique_id}"
