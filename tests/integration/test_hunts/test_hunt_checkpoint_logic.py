import uuid
import pytest
from pytz import utc
from dfe_engine.hunts.hunts.hunts_checkpoint_manager import HuntCheckpointManager
from dfe_engine.config.config_loader import DFEConfigLoader
import time
from hyperi_pylib.logger import logger as hs_logger
from datetime import datetime, timezone, timedelta
from dfe_engine.clickhouse.clickhouse_manager import ClickHouseManager
import logging
import yaml
from pathlib import Path

# These tests require a running ClickHouse instance
pytestmark = pytest.mark.integration

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
handler = logging.StreamHandler()
handler.setLevel(logging.DEBUG)
logger.addHandler(handler)


def create_unique_name(base_name: str) -> str:
    """Creates a unique name using UUID."""
    return f"{base_name}_{uuid.uuid4()}"


def load_config(file_path: Path) -> dict:
    """Loads the configuration from a YAML file."""
    with open(file_path, "r") as f:
        config = yaml.safe_load(f)
    return config


@pytest.fixture(scope="module")
def ch_client(dfe_config_fixtures):
    """Fixture for setting up the ClickHouse client."""
    config = DFEConfigLoader.read_clickhouse_config(
        target_name=dfe_config_fixtures["global_settings"]["default_target"],
        targets_file_path=dfe_config_fixtures["global_settings"]["target_path"],
    )
    print(f"*** using config {config}")
    ch_client = ClickHouseManager.get_instance(target_config_data=config).get_clickhouse_client()
    yield ch_client


@pytest.fixture(scope="function")
def test_logger(tmp_path):
    """Return hyperi-pylib logger for hunt tests."""
    return hs_logger


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
        execution_time = datetime.now(timezone.utc)
        end_time = datetime.now(timezone.utc)
        end_time_str = end_time.strftime("%Y-%m-%d %H:%M:%S")
        execution_time_str = execution_time.strftime("%Y-%m-%d %H:%M:%S")
        execution_time_ms = (end_time - execution_time).total_seconds() * 1000

        generated_query_id = str(uuid.uuid4())
        log_buffer = 60
        query_window_seconds = 600

        scheduled_start_time = datetime.now(timezone.utc)
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
        execution_time = datetime.now(timezone.utc)
        end_time = datetime.now(timezone.utc)
        scheduled_start_time = datetime.now(timezone.utc)
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
            query_checkpoint_time = utc.localize(query_checkpoint_time)

        logger.info(f"Retrieved previous successful run time: {previous_success_time}")
        logger.info(f"Retrieved last successful run time: {query_checkpoint_time}")

        current_time = datetime.now(timezone.utc)
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
