import asyncio
from concurrent.futures import ThreadPoolExecutor

import os
from ..runner.cron_runner import CronRunner


class HuntScheduler:
    def __init__(
        self,
        hunt_dir: str,
        rule_repo_dir: str,
        hunt_checkpoint_path: str,
        num_threads: int,
        hunt_log_path: str,
        checkpoint_destination: str,
        logger: logging.Logger,
        hunt_cron_task_timeout: int,
        checkpoint_timestamp_field: str,
        target_config_data: dict = None,
    ):
        self.hunt_dir = hunt_dir
        self.rule_repo_dir = rule_repo_dir
        self.hunt_checkpoint_path = hunt_checkpoint_path
        self.hunt_cron_task_timeout = hunt_cron_task_timeout
        self.num_threads = num_threads
        self.checkpoint_destination = checkpoint_destination
        self.hunt_log_path = hunt_log_path
        self.target_config_data = target_config_data
        self.checkpoint_timestamp_field = checkpoint_timestamp_field
        self.logger = logger

        with ThreadPoolExecutor() as executor:
            self.max_threads = max(self.num_threads, executor._max_workers)

    def print_hunt_parameters(self):
        # Note: No need to pass parameters that are available as instance attributes
        self.logger.info("\n--- Hunt Configuration Settings --- \n")
        self.logger.info(f"Hunt directory:                        {self.hunt_dir}")
        self.logger.info(
            f"Rule repository directory:             {self.rule_repo_dir}"
        )
        self.logger.info(
            f"Hunt Checkpoint Path:                  {self.hunt_checkpoint_path}"
        )
        self.logger.info(
            f"hunt_cron_task_timeout:                {self.hunt_cron_task_timeout} seconds"
        )
        self.logger.info(
            f"Number of threads:                     {self.num_threads}"
        )
        self.logger.info(
            f"Hunt Checkpoint Destination:           {self.checkpoint_destination}"
        )
        self.logger.info(
            f"Hunt logs directory:                   {self.hunt_log_path}"
        )

        try:
            rules = os.listdir(self.rule_repo_dir)
            self.logger.info(
                f"-- Loading Rules Rules from {self.rule_repo_dir} -- "
            )
            for entry in rules:
                entry_path = os.path.join(self.rule_repo_dir, entry)
                if os.path.isfile(entry_path):
                    self.logger.info(f"Loaded Hunt Rule: {entry}")
        except FileNotFoundError as e:
            self.logger.error(
                f"Error accessing directory '{self.rule_repo_dir}': {e}"
            )

        self.logger.info("-----------------------------------")

    def schedule_hunts_async(self):
        loop = asyncio.get_event_loop()
        loop.run_until_complete(self._run_schedule_with_hunts_threadpool())

    async def _run_schedule_with_hunts_threadpool(self):
        self.logger.info(
            "Scheduling hunts with {0} threads.".format(self.max_threads)
        )

        async def async_tasks():
            with ThreadPoolExecutor():
                tasks = [
                    asyncio.create_task(self._run_schedule_with_hunts())
                    for _ in range(self.max_threads)
                ]
                await asyncio.gather(*tasks)

        await async_tasks()

    async def _run_schedule_with_hunts(self):
        try:
            self.logger.info(
                f"DFE-Run-Cron with [{self.hunt_dir}] [{self.rule_repo_dir}]"
            )
            runner = CronRunner(
                hunt_dir=self.hunt_dir,
                rule_repo_dir=self.rule_repo_dir,
                hunt_checkpoint_path=self.hunt_checkpoint_path,
                hunt_cron_task_timeout=self.hunt_cron_task_timeout,
                checkpoint_destination=self.checkpoint_destination,
                checkpoint_timestamp_field=self.checkpoint_timestamp_field,
                hunt_log_path=self.hunt_log_path,
                target_config_data=self.target_config_data,
                logger=self.logger,
            )
            self.logger.info(f"DFE-Run-Cron setup[{runner}]")
            await runner.run()
        except Exception as e:
            self.logger.error(
                f"An error occurred scheduling the hunts: {e}", exc_info=True
            )
            raise
