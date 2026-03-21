import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from hyperi_pylib.logger import logger

from dfe_engine.clickhouse.clickhouse_manager import ClickHouseManager
from dfe_engine.hunts.checkpoint import HuntCheckpointManager
from dfe_engine.settings import get_settings

# These tests require a running ClickHouse instance
pytestmark = pytest.mark.integration


def create_unique_name(base_name: str) -> str:
    """Creates a unique name using UUID."""
    return f"{base_name}_{uuid.uuid4()}"


def load_config(file_path: Path) -> dict:
    """Loads the configuration from a YAML file."""
    with open(file_path) as f:
        config = yaml.safe_load(f)
    return config


@pytest.fixture(scope="module")
def ch_client():
    """Fixture for setting up the ClickHouse client."""
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


@pytest.fixture(scope="function")
def test_logger(tmp_path):
    """Return hyperi-pylib logger for hunt tests."""
    return logger


def test_ensure_table_exists(ch_client, unique_names):
    database_name, table_name = unique_names
    manager = HuntCheckpointManager(database_name=database_name, table_name=table_name)
    try:
        assert manager.ensure_table_exists(
            ch_client, create_missing_database=True, no_cluster_declarations_needed=True
        )
    finally:
        manager.drop_database_and_table(
            ch_client, database_name=database_name, table_name=table_name
        )


def test_ensure_database_not_exists(ch_client, unique_names):
    database_name, table_name = unique_names
    manager = HuntCheckpointManager(database_name=database_name, table_name=table_name)
    try:
        assert manager.ensure_table_exists(
            ch_client, create_missing_database=True, no_cluster_declarations_needed=True
        )
    finally:
        manager.drop_database_and_table(
            ch_client, database_name=database_name, table_name=table_name
        )


def test_ensure_table_not_exists(ch_client, unique_names):
    database_name, table_name = unique_names
    manager = HuntCheckpointManager(database_name=database_name, table_name=table_name)
    try:
        assert manager.ensure_table_exists(
            ch_client, create_missing_tables=True, no_cluster_declarations_needed=True
        )
    finally:
        manager.drop_database_and_table(
            ch_client, database_name=database_name, table_name=table_name
        )


hunt_name = create_unique_name("test_hunt")
customer = create_unique_name("detectionlab")
rule_name = create_unique_name("pc_posh_test_rule")


def test_create_and_update_checkpoint_success(ch_client, unique_names):
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

        manager.ensure_table_exists(
            ch_client, create_missing_database=True, no_cluster_declarations_needed=True
        )
        manager.create_checkpoint_clickhouse(
            ch_client=ch_client,
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

        time.sleep(3)
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


def test_create_and_update_checkpoint_success_windows_validation(ch_client, unique_names):
    database_name, table_name = unique_names
    manager = HuntCheckpointManager(database_name=database_name, table_name=table_name)

    try:
        hunt_name = "test_hunt"
        customer = "detectionlab"
        rule_name = "pc_posh_test_rule"

        manager.ensure_table_exists(
            ch_client, create_missing_database=True, no_cluster_declarations_needed=True
        )

        previous_success_time = None
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
            ch_client=ch_client,
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

        logger.info("Sleeping for 5 seconds before creating the next checkpoint...")
        time.sleep(5)

        query_checkpoint_time = manager.get_last_successful_run(
            ch_client=ch_client,
            hunt_name=hunt_name,
            rule_name=rule_name,
            customer=customer,
            checkpoint_destination="clickhouse",
        )

        assert query_checkpoint_time is not None

        if query_checkpoint_time.tzinfo is None:
            query_checkpoint_time = query_checkpoint_time.replace(tzinfo=UTC)

        logger.info(f"Retrieved previous successful run time: {previous_success_time}")
        logger.info(f"Retrieved last successful run time: {query_checkpoint_time}")

        current_time = datetime.now(UTC)
        time_diff = current_time - query_checkpoint_time
        logger.info(f"Time difference between checkpoints: {time_diff.total_seconds()} seconds")

        if not (5 <= time_diff.total_seconds() < 10):
            logger.warning(
                f"Checkpoints not spaced by 5 to 10 seconds: Actual time difference is {time_diff.total_seconds()} seconds"
            )

        logger.info(
            f"Completed test for checkpoint creation and window validation for hunt {hunt_name}"
        )

    finally:
        manager.drop_database_and_table(
            ch_client, database_name=database_name, table_name=table_name
        )
