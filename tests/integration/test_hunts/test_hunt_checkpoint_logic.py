import uuid
from datetime import UTC, datetime, timedelta

import pytest

from dfe_engine.hunts.checkpoint import HuntCheckpointManager

# These tests require a running ClickHouse instance
pytestmark = pytest.mark.integration


def create_unique_name(base_name: str) -> str:
    """Creates a unique name using UUID."""
    return f"{base_name}_{uuid.uuid4()}"


def test_ensure_table_exists(manager_client, unique_names):
    database_name, table_name = unique_names
    manager = HuntCheckpointManager(database_name=database_name, table_name=table_name)
    try:
        assert manager.ensure_table_exists(manager_client, create_missing_database=True)
    finally:
        manager.drop_database_and_table(
            manager_client, database_name=database_name, table_name=table_name
        )


def test_ensure_database_not_exists(manager_client, unique_names):
    database_name, table_name = unique_names
    manager = HuntCheckpointManager(database_name=database_name, table_name=table_name)
    try:
        assert manager.ensure_table_exists(manager_client, create_missing_database=True)
    finally:
        manager.drop_database_and_table(
            manager_client, database_name=database_name, table_name=table_name
        )


def test_ensure_table_not_exists(manager_client, unique_names):
    database_name, table_name = unique_names
    manager = HuntCheckpointManager(database_name=database_name, table_name=table_name)
    try:
        assert manager.ensure_table_exists(manager_client, create_missing_tables=True)
    finally:
        manager.drop_database_and_table(
            manager_client, database_name=database_name, table_name=table_name
        )


hunt_name = create_unique_name("test_hunt")
customer = create_unique_name("detectionlab")
rule_name = create_unique_name("pc_posh_test_rule")


def test_create_and_update_checkpoint_success(manager_client, unique_names):
    database_name, table_name = unique_names
    manager = HuntCheckpointManager(database_name=database_name, table_name=table_name)

    try:
        execution_time = datetime.now(UTC)
        end_time = datetime.now(UTC)
        end_time_str = end_time.strftime("%Y-%m-%d %H:%M:%S")
        execution_time_str = execution_time.strftime("%Y-%m-%d %H:%M:%S")
        execution_time_ms = (end_time - execution_time).total_seconds() * 1000

        generated_query_id = str(uuid.uuid4())
        log_buffer = 60
        query_window_seconds = 600

        scheduled_start_time = datetime.now(UTC)
        last_success_time = scheduled_start_time - timedelta(seconds=query_window_seconds)
        scheduled_start_time_w_buffer = scheduled_start_time - timedelta(seconds=log_buffer)
        scheduled_start_time_w_buffer_str = scheduled_start_time_w_buffer.strftime(
            "%Y-%m-%d %H:%M:%S"
        )

        manager.ensure_table_exists(manager_client, create_missing_database=True)
        manager.create_checkpoint_clickhouse(
            ch_client=manager_client,
            customer=customer,
            rule=rule_name,
            hunt_name=hunt_name,
            log_buffer=log_buffer,
            thread_id="11111",
            previous_successful_checkpoint_str=last_success_time.strftime("%Y-%m-%d %H:%M:%S"),
            query_schedule_time_str=scheduled_start_time.strftime("%Y-%m-%d %H:%M:%S"),
            execution_time_str=execution_time_str,
            execution_time_ms=execution_time_ms,
            end_time_str=end_time_str,
            query_checkpoint_time_str=scheduled_start_time_w_buffer_str,
            query_id=generated_query_id,
        )
        assert execution_time_str is not None

        query_checkpoint_time = manager.get_last_successful_run_clickhouse(
            ch_client=manager_client,
            hunt_name=hunt_name,
            rule_name=rule_name,
            customer=customer,
        )
        assert query_checkpoint_time is not None

    finally:
        manager.drop_database_and_table(
            manager_client, database_name=database_name, table_name=table_name
        )


def test_create_and_update_checkpoint_success_windows_validation(manager_client, unique_names):
    database_name, table_name = unique_names
    manager = HuntCheckpointManager(database_name=database_name, table_name=table_name)

    try:
        hunt_name = "test_hunt"
        customer = "detectionlab"
        rule_name = "pc_posh_test_rule"

        manager.ensure_table_exists(manager_client, create_missing_database=True)

        execution_time = datetime.now(UTC)
        end_time = datetime.now(UTC)
        scheduled_start_time = datetime.now(UTC)
        execution_time_str = execution_time.strftime("%Y-%m-%d %H:%M:%S")
        end_time_str = end_time.strftime("%Y-%m-%d %H:%M:%S")
        execution_time_ms = (end_time - execution_time).total_seconds() * 1000
        log_buffer = 60
        query_window_seconds = 600
        last_success_time = scheduled_start_time - timedelta(seconds=query_window_seconds)
        generated_query_id = str(uuid.uuid4())
        scheduled_start_time_w_buffer = scheduled_start_time - timedelta(seconds=log_buffer)
        scheduled_start_time_w_buffer_str = scheduled_start_time_w_buffer.strftime(
            "%Y-%m-%d %H:%M:%S"
        )

        manager.create_checkpoint_clickhouse(
            ch_client=manager_client,
            customer=customer,
            rule=rule_name,
            hunt_name=hunt_name,
            log_buffer=log_buffer,
            thread_id="11111",
            previous_successful_checkpoint_str=last_success_time.strftime("%Y-%m-%d %H:%M:%S"),
            query_schedule_time_str=scheduled_start_time.strftime("%Y-%m-%d %H:%M:%S"),
            execution_time_str=execution_time_str,
            execution_time_ms=execution_time_ms,
            end_time_str=end_time_str,
            query_checkpoint_time_str=scheduled_start_time_w_buffer_str,
            query_id=generated_query_id,
        )

        query_checkpoint_time = manager.get_last_successful_run(
            ch_client=manager_client,
            hunt_name=hunt_name,
            rule_name=rule_name,
            customer=customer,
            checkpoint_destination="clickhouse",
        )

        assert query_checkpoint_time is not None

        if query_checkpoint_time.tzinfo is None:
            query_checkpoint_time = query_checkpoint_time.replace(tzinfo=UTC)
        # The window read back is the one written: the schedule time less the log buffer.
        assert query_checkpoint_time == scheduled_start_time_w_buffer.replace(microsecond=0)

    finally:
        manager.drop_database_and_table(
            manager_client, database_name=database_name, table_name=table_name
        )
