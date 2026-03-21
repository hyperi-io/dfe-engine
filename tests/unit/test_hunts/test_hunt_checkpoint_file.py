import os
import shutil
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from hyperi_pylib.logger import logger

from dfe_engine.hunts.checkpoint import HuntCheckpointManager

execution_time = datetime.now(UTC)
execution_time_str = execution_time.strftime("%Y-%m-%d %H:%M:%S")


@pytest.fixture
def setup_paths(tmp_path):
    paths = [tmp_path / "logs", tmp_path / "data"]

    for path in paths:
        os.makedirs(path, exist_ok=True)

    yield paths
    for path in paths:
        if path.exists() and path.is_dir():
            shutil.rmtree(path)


@pytest.fixture
def setup_hunt_logger(setup_paths):
    """Return hyperi-pylib logger for hunt tests."""
    return logger


@pytest.fixture
def create_checkpoint_manager():
    return HuntCheckpointManager()


@pytest.mark.parametrize("hunt_name, rule_name", [("test_hunt", "pc_posh_test_rule")])
def test_create_update_checkpoint(
    hunt_name,
    rule_name,
    setup_paths,
    setup_hunt_logger,
    create_checkpoint_manager,
):
    customer = "detectionlab"
    end_time = datetime.now(UTC)
    scheduled_start_time = datetime.now(UTC)
    execution_time_ms = (end_time - execution_time).total_seconds() * 1000
    generated_query_id = str(uuid.uuid4())
    log_buffer = 60
    query_window_seconds = 600
    scheduled_start_time_w_buffer = scheduled_start_time - timedelta(seconds=log_buffer)
    last_success_time = scheduled_start_time - timedelta(seconds=query_window_seconds)
    file_path = setup_paths[1] / "hunt_checkpoints.json"
    Path(file_path).touch()

    create_checkpoint_manager.create_checkpoint_file(
        customer=customer,
        rule=rule_name,
        hunt_name=hunt_name,
        thread_id="111",
        log_buffer=60,
        previous_successful_checkpoint_str=last_success_time.strftime("%Y-%m-%d %H:%M:%S"),
        query_schedule_time_str=scheduled_start_time.strftime("%Y-%m-%d %H:%M:%S"),
        execution_time_str=execution_time.strftime("%Y-%m-%d %H:%M:%S"),
        execution_time_ms=execution_time_ms,
        end_time_str=end_time.strftime("%Y-%m-%d %H:%M:%S"),
        query_checkpoint_time_str=scheduled_start_time_w_buffer.strftime("%Y-%m-%d %H:%M:%S"),
        file_path=file_path,
        query_id=generated_query_id,
    )
    time.sleep(3)

    last_success_time = create_checkpoint_manager.get_last_successful_run_file(
        hunt_name=hunt_name,
        rule_name=rule_name,
        file_path=file_path,
        customer=customer,
    )

    assert last_success_time is not None, (
        f"Expected last_success_time to be not None, but got {last_success_time}. "
        f"File: {file_path}, Content: {file_path.read_text(errors='ignore')}"
    )
    # Note: hyperi_pylib logger (structlog) doesn't write to pytest caplog,
    # so we verify success by checking last_success_time is not None above
