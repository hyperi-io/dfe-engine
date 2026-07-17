"""Integration tests for the ClickHouse startup bootstrap against a real ClickHouse."""

import pytest

from dfe_engine.clickhouse.bootstrap import bootstrap_clickhouse
from dfe_engine.clickhouse.clickhouse_manager import ClickHouseManager
from dfe_engine.schema.engine_resolver import EngineResolver, parse_engine
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


class TestBootstrapMatchesServerTopology:
    """The bootstrap must create tables shaped for the server it is talking to.

    Adaptive across the CH target matrix (local single / cluster / cloud): the
    expectation is derived by sensing the live server, not hardcoded. The 2026-07-16
    dfe-k8s deploy created plain MergeTree tables on a 3-replica cluster - each node
    independently, unreplicated - because the bootstrap never sensed. Rows then
    scattered across replicas and reads returned whatever the connection happened to
    land on.
    """

    def test_engine_matches_sensed_topology(self, clickhouse_test_database):
        client = _bootstrap_and_client(clickhouse_test_database)
        sensed = EngineResolver(client=client).resolve(
            parse_engine("MergeTree"), clickhouse_test_database
        )
        rows = client.query(
            f"SELECT engine FROM system.tables WHERE database = '{clickhouse_test_database}' "
            "AND name = 'default'"
        ).result_rows
        assert rows, "bootstrap did not create the default table"
        engine = rows[0][0]
        if sensed.topology == "replicated":
            assert engine.startswith("Replicated"), (
                f"server sensed as replicated but table was created as {engine!r} - "
                "data will silently split across replicas"
            )
        else:
            assert engine == "MergeTree"

    def test_table_exists_on_every_replica(self, clickhouse_test_database):
        """On a real cluster the table must exist on ALL nodes, not just the one
        the bootstrap connection landed on. clusterAllReplicas fans the check out.
        """
        client = _bootstrap_and_client(clickhouse_test_database)
        sensed = EngineResolver(client=client)
        resolved = sensed.resolve(parse_engine("MergeTree"), clickhouse_test_database)
        if not resolved.on_cluster:
            pytest.skip("not an ON CLUSTER deployment - single-node/Replicated-db target")
        cluster = resolved.on_cluster.removeprefix(" ON CLUSTER ")
        # Both counts fan out the same way: system.one yields exactly one row per
        # reachable node, so it is the node count. (system.clusters is NOT a valid
        # denominator - it can list a replica more than once, e.g. one row per
        # configured port.)
        replicas = client.query(
            f"SELECT count() FROM clusterAllReplicas('{cluster}', system.one)"
        ).result_rows[0][0]
        found = client.query(
            f"SELECT count() FROM clusterAllReplicas('{cluster}', system.tables) "
            f"WHERE database = '{clickhouse_test_database}' AND name = 'default'"
        ).result_rows[0][0]
        assert found == replicas, f"default table on {found}/{replicas} replicas"
