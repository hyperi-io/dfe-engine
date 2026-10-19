"""Integration tests for the ClickHouse startup bootstrap against a real ClickHouse.

Everything DFE writes lands in the one data database -- the landing table, hunt
output and the engine's own state -- so both tables are asserted there.
"""

import pytest
from dfe_schemas.clickhouse import Platform, PlatformProbe

from dfe_engine.clickhouse.bootstrap import bootstrap_clickhouse
from dfe_engine.clickhouse.clickhouse_manager import ClickHouseManager
from dfe_engine.settings import get_clickhouse_config
from tests.support.engine_stack import bootstrap_settings

pytestmark = pytest.mark.integration

# A Merge table stores no rows, so no replica can hold data another lacks.
DATALESS_ENGINES = ("Merge",)


def _table_names(*, client, database) -> set[str]:
    rows = client.query(f"SELECT name FROM system.tables WHERE database = '{database}'").result_rows
    return {row[0] for row in rows}


@pytest.fixture
def bootstrap(clickhouse_test_database, ch_params):
    """Bootstrap the DB, then return the SAME manager connection it used.

    Keeping create and verify on the bootstrap's own connection means a
    load-balanced multi-node target answers from the node that was written.
    """

    def run():
        ClickHouseManager.reset_instance()
        settings = bootstrap_settings(ch_params=ch_params, database=clickhouse_test_database)
        bootstrap_clickhouse(settings=settings)
        manager = ClickHouseManager.get_instance(get_clickhouse_config(settings=settings))
        return manager.get_clickhouse_client()

    try:
        yield run
    finally:
        ClickHouseManager.reset_instance()


class TestBootstrapClickhouse:
    def test_creates_the_landing_and_detection_tables(self, clickhouse_test_database, bootstrap):
        client = bootstrap()
        tables = _table_names(client=client, database=clickhouse_test_database)
        assert {"main", "detection"} <= tables

    def test_is_idempotent(self, clickhouse_test_database, bootstrap):
        bootstrap()
        # A second bootstrap must not error and the tables stay present.
        client = bootstrap()
        tables = _table_names(client=client, database=clickhouse_test_database)
        assert {"main", "detection"} <= tables


class TestBootstrapTakesTheOneDDLShape:
    """Every table the bootstrap creates is replicated, on every replica.

    A clustered deploy once created plain MergeTree tables on a 3-replica cluster,
    each node independently and unreplicated. Rows then scattered across replicas
    and reads returned whatever the connection happened to land on.
    """

    def test_every_table_takes_a_replicated_engine(self, clickhouse_test_database, bootstrap):
        client = bootstrap()
        rows = client.query(
            "SELECT name, engine FROM system.tables WHERE database = {db:String} "
            "AND engine NOT IN ('View', 'MaterializedView')",
            parameters={"db": clickhouse_test_database},
        ).result_rows
        assert rows, "bootstrap created no table"
        plain = [
            (name, engine)
            for name, engine in rows
            if not engine.startswith("Replicated") and engine not in DATALESS_ENGINES
        ]
        assert not plain, f"tables that would split across replicas: {plain}"

    def test_table_exists_on_every_replica(self, clickhouse_test_database, bootstrap):
        """The table is on ALL nodes, not just the one the bootstrap connection reached."""
        client = bootstrap()
        cluster = client.query("SELECT getMacro('cluster')").result_rows[0][0]
        # system.one yields exactly one row per reachable node, so it is the node count.
        # system.clusters is not, because it can list a replica once per configured port.
        replicas = client.query(
            f"SELECT count() FROM clusterAllReplicas('{cluster}', system.one)"
        ).result_rows[0][0]
        found = client.query(
            f"SELECT count() FROM clusterAllReplicas('{cluster}', system.tables) "
            f"WHERE database = '{clickhouse_test_database}' AND name = 'main'"
        ).result_rows[0][0]
        assert found == replicas, f"landing table on {found}/{replicas} replicas"


def test_the_harness_probes_as_self_hosted_with_keeper(ch_client):
    """No SharedMergeTree engine, and the Keeper and macros the Replicated database needs."""
    probe = PlatformProbe(ch_client)

    assert probe.platform is Platform.SELF_HOSTED
    assert probe.origin == "probe"
    probe.require_replication()
