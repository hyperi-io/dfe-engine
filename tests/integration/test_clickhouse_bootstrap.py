"""Integration tests for the ClickHouse startup bootstrap against a real ClickHouse."""

import pytest

from dfe_engine.clickhouse.bootstrap import bootstrap_clickhouse
from dfe_engine.clickhouse.clickhouse_manager import ClickHouseManager
from dfe_engine.settings import load_settings

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


class TestBootstrapClickhouse:
    def test_creates_default_and_hunt_results_tables(
        self, clickhouse_client, clickhouse_test_database
    ):
        ClickHouseManager.reset_instance()
        bootstrap_clickhouse(settings=_bootstrap_settings(database=clickhouse_test_database))
        names = _table_names(client=clickhouse_client, database=clickhouse_test_database)
        assert "default" in names
        assert "hunt_results" in names

    def test_is_idempotent(self, clickhouse_client, clickhouse_test_database):
        ClickHouseManager.reset_instance()
        settings = _bootstrap_settings(database=clickhouse_test_database)
        bootstrap_clickhouse(settings=settings)
        bootstrap_clickhouse(settings=settings)
        assert {"default", "hunt_results"} <= _table_names(
            client=clickhouse_client, database=clickhouse_test_database
        )
