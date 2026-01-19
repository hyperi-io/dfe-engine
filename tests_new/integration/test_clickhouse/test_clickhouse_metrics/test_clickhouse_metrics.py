import pytest
from dfe_engine.clickhouse.clickhouse_manager import ClickHouseManager
from dfe_engine.clickhouse.clickhouse_metrics import ClickHouseMetrics
from dfe_engine.hunts.hunts.hunts_checkpoint_manager import HuntCheckpointManager

def test_get_hunt_metrics(dfe_config_integration_target, clickhouse_environment_setup, test_get_hunt_metrics):
    clickhouse_manager = ClickHouseManager.get_instance(dfe_config_integration_target)
    ch_client = clickhouse_manager.get_clickhouse_client()
    clickhouse_metrics = ClickHouseMetrics(dfe_config_integration_target)

    hunt_name = test_get_hunt_metrics["hunt_name"]
    data_to_insert = test_get_hunt_metrics["data_to_insert"]
    expected_result = test_get_hunt_metrics["expected_result"]

    hunt_checkpoint_manager = HuntCheckpointManager()
    hunt_checkpoint_manager.ensure_table_exists(ch_client)

    rows
    clickhouse_manager.

    result = clickhouse_metrics.get_hunt_metrics(hunt_name)

    assert(result == expected_result)




    # single hunt
    # multiple hunts
    # no hunts
    # different data types