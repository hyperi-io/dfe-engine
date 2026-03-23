import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from dfe_engine.clickhouse.clickhouse_manager import ClickHouseManager
from dfe_engine.hunts.checkpoint import HuntCheckpointManager
from dfe_engine.settings import get_settings

# These tests require a running ClickHouse instance
pytestmark = pytest.mark.integration


@pytest.fixture
def setup_paths(tmp_path) -> dict:
    """Fixture to set up necessary paths for hunts and ensure cleanup."""
    dfe_root_log_path = tmp_path / "logs_path"
    dfe_root_log_path.mkdir(parents=True, exist_ok=True)

    return {"tmp/logs": dfe_root_log_path}


@pytest.fixture(scope="module")
def ch_client():
    settings = get_settings()
    config = {
        "ch_host": settings.clickhouse.host,
        "ch_port": settings.clickhouse.port,
        "ch_username": settings.clickhouse.username,
        "ch_password": settings.clickhouse.password,
        "ch_secure": settings.clickhouse.secure,
        "ch_verify": settings.clickhouse.verify,
    }
    ch_client = ClickHouseManager.get_instance(target_config_data=config).get_clickhouse_client()
    return ch_client


@pytest.mark.parametrize(
    "log_prefix, ensure_kwargs, expected_result",
    [
        (
            "test-checkpoint-hunt-schema",
            {"create_missing_database": True, "no_cluster_declarations_needed": True},
            True,
        ),
        (
            "dfe-scheduler-hunt",
            {"create_missing_database": True, "no_cluster_declarations_needed": True},
            True,
        ),
        (
            "dfe-scheduler-hunt",
            {"create_missing_tables": True, "no_cluster_declarations_needed": True},
            True,
        ),
    ],
)
def test_ensure_table_exists(
    log_prefix, unique_names, ensure_kwargs, expected_result, ch_client, setup_paths
):
    database_name, table_name = unique_names
    setup_paths["tmp/logs"]
    manager = HuntCheckpointManager(database_name=database_name, table_name=table_name)
    max_retries = 3
    retries = 0
    success = False

    try:
        while retries < max_retries:
            try:
                result = manager.ensure_table_exists(ch_client, **ensure_kwargs)
                assert result == expected_result
                success = True
                break
            except FileNotFoundError:
                retries += 1
                if retries < max_retries:
                    time.sleep(0.5)
                else:
                    pytest.fail(
                        f"Test failed after {max_retries} retries due to missing file or directory."
                    )
    finally:
        if success:
            manager.drop_database_and_table(
                ch_client, database_name=database_name, table_name=table_name
            )
        else:
            manager.drop_database_and_table(
                ch_client, database_name=database_name, table_name=table_name
            )
            if retries == max_retries:
                pytest.fail("Failed to ensure table exists after maximum retries.")


@pytest.mark.parametrize("hunt_name, rule_name", [("test_hunt", "pc_posh_test_rule")])
def test_create_checkpoint(hunt_name, rule_name, ch_client, setup_paths, unique_names):
    database_name, table_name = unique_names
    manager = HuntCheckpointManager(database_name=database_name, table_name=table_name)
    try:
        customer = "detectionlab"
        generated_query_id = str(uuid.uuid4())
        log_buffer = 60
        query_window_seconds = 600

        execution_time = datetime.now(UTC)
        end_time = datetime.now(UTC)
        scheduled_start_time = datetime.now(UTC)
        execution_time_ms = (end_time - execution_time).total_seconds() * 1000
        scheduled_start_time_w_buffer = scheduled_start_time - timedelta(seconds=log_buffer)
        last_success_time = scheduled_start_time - timedelta(seconds=query_window_seconds)

        manager.ensure_table_exists(
            ch_client, create_missing_database=True, no_cluster_declarations_needed=True
        )
        manager.checkpoint_rule(
            ch_client=ch_client,
            customer=customer,
            rule=rule_name,
            checkpoint_destination="clickhouse",
            log_buffer=60,
            hunt_name=hunt_name,
            execution_time_str=execution_time.strftime("%Y-%m-%d %H:%M:%S"),
            execution_time_ms=execution_time_ms,
            end_time_str=end_time.strftime("%Y-%m-%d %H:%M:%S"),
            previous_successful_checkpoint_str=last_success_time.strftime("%Y-%m-%d %H:%M:%S"),
            query_checkpoint_time_str=scheduled_start_time_w_buffer.strftime("%Y-%m-%d %H:%M:%S"),
            query_id=generated_query_id,
            query_schedule_time_str=scheduled_start_time.strftime("%Y-%m-%d %H:%M:%S"),
        )
        time.sleep(5)

        query_checkpoint_time = manager.get_last_successful_run_clickhouse(
            ch_client=ch_client,
            hunt_name=hunt_name,
            rule_name=rule_name,
            customer=customer,
        )
        assert query_checkpoint_time is not None
    finally:
        manager.drop_database_and_table(
            ch_client, database_name=database_name, table_name=table_name
        )


@pytest.mark.parametrize("num_records", [10, 100, 20000])
def test_create_batch_checkpoint(num_records: int, ch_client, setup_paths, unique_names):
    database_name, table_name = unique_names
    manager = HuntCheckpointManager(database_name=database_name, table_name=table_name)
    try:
        customer = "detectionlab"
        log_buffer = 60
        query_window_seconds = 600

        manager.ensure_table_exists(
            ch_client, create_missing_database=True, no_cluster_declarations_needed=True
        )

        checkpoints = []
        now = datetime.now(UTC)

        for _ in range(num_records):
            generated_query_id = str(uuid.uuid4())
            execution_time = now
            end_time = now + timedelta(milliseconds=500)
            scheduled_start_time = now + timedelta(seconds=10)
            execution_time_ms = (end_time - execution_time).total_seconds() * 1000
            scheduled_start_time_w_buffer = scheduled_start_time - timedelta(seconds=log_buffer)
            last_success_time = scheduled_start_time - timedelta(seconds=query_window_seconds)

            checkpoint = {
                "customer_name": customer,
                "rule_name": "pc_posh_test_rule_larger_name_" + str(generated_query_id),
                "hunt_name": "test_hunt_batch" + str(generated_query_id),
                "query_id": generated_query_id,
                "log_buffer": log_buffer,
                "query_schedule_time": scheduled_start_time.strftime("%Y-%m-%d %H:%M:%S"),
                "execution_time": execution_time.strftime("%Y-%m-%d %H:%M:%S"),
                "end_time": end_time.strftime("%Y-%m-%d %H:%M:%S"),
                "previous_successful_checkpoint": last_success_time.strftime("%Y-%m-%d %H:%M:%S"),
                "query_checkpoint_time": scheduled_start_time_w_buffer.strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
                "execution_time_ms": execution_time_ms,
                "thread_id": "",
            }

            checkpoints.append(checkpoint)

        manager.create_batch_checkpoint_clickhouse(ch_client, checkpoints)

        time.sleep(5)
        for checkpoint in checkpoints[0:5]:
            query_checkpoint_time = manager.get_last_successful_run_clickhouse(
                ch_client=ch_client,
                hunt_name=checkpoint["hunt_name"],
                rule_name=checkpoint["rule_name"],
                customer=customer,
            )
            assert query_checkpoint_time is not None

    finally:
        manager.drop_database_and_table(
            ch_client, database_name=database_name, table_name=table_name
        )
