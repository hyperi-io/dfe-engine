import asyncio
import os

import pytest
from hyperi_pylib.logger import logger

from dfe_engine.hunts.cron_runner import CronRunner
from dfe_engine.settings import get_settings

dfe_root_log_path = os.path.join(os.getcwd(), "tmp", "logs_path")
os.makedirs(dfe_root_log_path, exist_ok=True)


async def run_schedule_with_hunts(dfe_config_fixtures, setup_paths, thread_id):
    logger.info(f"Thread {thread_id}: Hunt running")
    start_time = asyncio.get_running_loop().time()
    settings = get_settings()
    target_config_data = {
        "ch_host": settings.clickhouse.host,
        "ch_port": settings.clickhouse.port,
        "ch_username": settings.clickhouse.username,
        "ch_password": settings.clickhouse.password,
        "ch_secure": settings.clickhouse.secure,
        "ch_verify": settings.clickhouse.verify,
        "target_name": "test",
    }
    runner = CronRunner(
        hunt_dir=dfe_config_fixtures["hunt_scheduler"]["hunt_dir"],
        rule_repo_dir=dfe_config_fixtures["hunt_scheduler"]["rule_repo_dir"],
        hunt_cron_task_timeout=70,
        checkpoint_destination="clickhouse",
        hunt_checkpoint_path=dfe_config_fixtures["global_settings"]["hunt_checkpoint_path"],
        hunt_log_path=setup_paths[1],
        checkpoint_timestamp_field="timestamp",
        target_config_data=target_config_data,
    )
    await runner.run_async()
    end_time = asyncio.get_running_loop().time()
    execution_time = end_time - start_time
    logger.info(f"Thread {thread_id}: Hunt finished in {execution_time} seconds")

    return execution_time


async def run_schedule_with_hunts_threadpool(dfe_config_fixtures, setup_paths):
    asyncio.get_event_loop()
    threads_info = []

    async def run_schedule_with_hunts_async(thread_id):
        return await run_schedule_with_hunts(dfe_config_fixtures, setup_paths, thread_id)

    tasks = [
        run_schedule_with_hunts_async(thread_id)
        for thread_id in range(dfe_config_fixtures["hunt_scheduler"]["num_threads"])
    ]

    execution_times = await asyncio.gather(*tasks)

    threads_info = [
        {"thread_id": thread_id, "execution_time": execution_time}
        for thread_id, execution_time in enumerate(execution_times)
    ]
    return threads_info


@pytest.mark.asyncio
async def test_async_run_schedule_with_hunts_threadpool(dfe_config_fixtures, setup_paths):
    threads_info = await run_schedule_with_hunts_threadpool(dfe_config_fixtures, setup_paths)
    logger.info("Summary:")
    logger.info(f"Total threads used: {len(threads_info)}")
    for thread_info in threads_info:
        logger.info(
            f"Thread {thread_info['thread_id']}: Execution Time: {thread_info['execution_time']} seconds"
        )

    params = dfe_config_fixtures["hunt_scheduler"]
    assert len(threads_info) == params["num_threads"]


@pytest.mark.asyncio
async def test_thread_analysis_benchmark(dfe_config_fixtures, setup_paths):
    total_threads = 0
    total_execution_time = 0

    logger.info("Starting benchmark analysis...")

    threads_info = await run_schedule_with_hunts_threadpool(dfe_config_fixtures, setup_paths)
    total_threads = len(threads_info)
    total_execution_time = sum(thread_info["execution_time"] for thread_info in threads_info)
    average_execution_time = total_execution_time / (total_threads)

    logger.info("Benchmark analysis completed .")
    logger.info(f"Total threads used: {total_threads}")
    logger.info(f"Total execution time: {total_execution_time} seconds")
    logger.info(f"Average execution time per thread: {average_execution_time} seconds")

    params = dfe_config_fixtures["hunt_scheduler"]
    assert total_threads == params["num_threads"]
    assert total_execution_time > 0
    assert average_execution_time > 0
