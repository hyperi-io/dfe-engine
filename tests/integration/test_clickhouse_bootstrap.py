"""Integration tests for the ClickHouse startup bootstrap against a real ClickHouse."""

import pytest

from dfe_engine.clickhouse.bootstrap import bootstrap_clickhouse
from dfe_engine.clickhouse.clickhouse_manager import ClickHouseManager
from dfe_engine.settings import get_clickhouse_config, load_settings

pytestmark = pytest.mark.integration


def _bootstrap_settings(*, database):
    settings = load_settings()
    settings.clickhouse.data_database = database
    settings.clickhouse.secure = False
    settings.clickhouse.bootstrap_tables = True
    return settings


def _table_names(*, client, database) -> set[str]:
    rows = client.query(f"SELECT name FROM system.tables WHERE database = '{database}'").result_rows
    return {row[0] for row in rows}


def _bootstrap_and_client(database):
    """Bootstrap the DB, then return the SAME manager connection it used.

    On a load-balanced multi-node cluster a SEPARATE connection may hit a
    different node where a single-topology (non-replicated) table is not visible;
    keeping create + verify on the bootstrap's own connection makes the assertion
    deterministic (fixes the multi-node flake - it is a wrong-setup, not a flake).
    """
    ClickHouseManager.reset_instance()
    settings = _bootstrap_settings(database=database)
    bootstrap_clickhouse(settings=settings)
    manager = ClickHouseManager.get_instance(get_clickhouse_config(settings=settings))
    return manager.get_clickhouse_client()


class TestBootstrapClickhouse:
    def test_creates_default_and_hunt_results_tables(self, clickhouse_test_database):
        client = _bootstrap_and_client(clickhouse_test_database)
        names = _table_names(client=client, database=clickhouse_test_database)
        assert "default" in names
        assert "hunt_results" in names

    def test_is_idempotent(self, clickhouse_test_database):
        client = _bootstrap_and_client(clickhouse_test_database)
        # A second bootstrap must not error and the tables stay present.
        bootstrap_clickhouse(settings=_bootstrap_settings(database=clickhouse_test_database))
        assert {"default", "hunt_results"} <= _table_names(
            client=client, database=clickhouse_test_database
        )
