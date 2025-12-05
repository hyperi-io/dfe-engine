import uuid
import pytest
import time
import os
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from dfecli.dfe_async_hunts.hunts.hunts_checkpoint_manager import HuntCheckpointManager
from dfecli.dfe_logger.dfe_logger import DFELog
import shutil

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
handler = logging.StreamHandler()
handler.setLevel(logging.DEBUG)
logger.addHandler(handler)

execution_time = datetime.now(timezone.utc)
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
def setup_hunt_logger(setup_paths) -> logging.Logger:
    log_path = setup_paths[0]
    return DFELog.get_root_logger(
        logging_directory=log_path, log_file_prefix="dfe-scheduler-hunt"
    )


@pytest.fixture
def create_checkpoint_manager(setup_hunt_logger):
    return HuntCheckpointManager(setup_hunt_logger)


@pytest.mark.parametrize("hunt_name, rule_name", [("test_hunt", "pc_posh_test_rule")])
def test_create_update_checkpoint(
    hunt_name,
    rule_name,
    setup_paths,
    setup_hunt_logger,
    create_checkpoint_manager,
    caplog,
):
    customer = "detectionlab"
    end_time = datetime.now(timezone.utc)
    scheduled_start_time = datetime.now(timezone.utc)
    execution_time_ms = (end_time - execution_time).total_seconds() * 1000
    generated_query_id = str(uuid.uuid4())
    log_buffer = 60
    query_window_seconds = 600
    scheduled_start_time_w_buffer = scheduled_start_time - timedelta(seconds=log_buffer)
    last_success_time = scheduled_start_time - timedelta(seconds=query_window_seconds)
    file_path = setup_paths[1] / "hunt_checkpoints.json"
    Path(file_path).touch()

    with caplog.at_level(logging.INFO):
        create_checkpoint_manager.create_checkpoint_file(
            customer=customer,
            rule=rule_name,
            hunt_name=hunt_name,
            thread_id="111",
            log_buffer=60,
            previous_successful_checkpoint_str=last_success_time.strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
            query_schedule_time_str=scheduled_start_time.strftime("%Y-%m-%d %H:%M:%S"),
            execution_time_str=execution_time.strftime("%Y-%m-%d %H:%M:%S"),
            execution_time_ms=execution_time_ms,
            end_time_str=end_time.strftime("%Y-%m-%d %H:%M:%S"),
            query_checkpoint_time_str=scheduled_start_time_w_buffer.strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
            dfe_logger=setup_hunt_logger,
            file_path=file_path,
            query_id=generated_query_id,
        )
    time.sleep(3)

    last_success_time = create_checkpoint_manager.get_last_successful_run_file(
        hunt_name=hunt_name,
        rule_name=rule_name,
        file_path=file_path,
        customer=customer,
        dfe_logger=setup_hunt_logger,
    )

    assert last_success_time is not None, (
        f"Expected last_success_time to be not None, but got {last_success_time}. "
        f"File: {file_path}, Content: {file_path.read_text(errors='ignore')}"
    )
    assert any(
        "Checkpoint created successfully" in message.message
        for message in caplog.records
    )
