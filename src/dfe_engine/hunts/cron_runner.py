import asyncio
import os
import threading
import time
from datetime import UTC, datetime

from hyperi_pylib.logger import logger
from jinja2 import Environment, FileSystemLoader

from ..settings import get_settings
from .cron_job import CronJob


class CronRunner:
    CRON_LOG_FILE_NAME = "hunt-cron-runner-logs"
    DEFAULT_HUNT_LOG_FILE_PATH = "hunt_log_path"

    THREAD_TRACKING_LOG = "thread_tracking.log"
    MAX_LOG_SIZE_BYTES = 100 * 1024 * 1024  # 100 MB
    BACKUP_COUNT = 3

    def __init__(
        self,
        hunt_dir: str,
        rule_repo_dir: str,
        hunt_checkpoint_path: str,
        hunt_cron_task_timeout: int,
        checkpoint_destination: str,
        hunt_log_path: str | None = None,
        checkpoint_timestamp_field: str | None = None,
        target_config_data: dict | None = None,
    ):
        self.hunt_dir = hunt_dir
        self.rule_repo_dir = rule_repo_dir
        self.hunt_checkpoint_path = hunt_checkpoint_path
        self.hunt_cron_task_timeout = hunt_cron_task_timeout
        self.checkpoint_destination = checkpoint_destination
        self.target_config_data = target_config_data
        self.checkpoint_timestamp_field = checkpoint_timestamp_field
        self.cron_runner = None
        self.daemon_pid = None
        self.scheduler_lock = threading.Lock()
        if hunt_log_path:
            self.hunt_log_path = hunt_log_path
        else:
            settings = get_settings()
            self.hunt_log_path = settings.hunts.log_path or CronRunner.DEFAULT_HUNT_LOG_FILE_PATH
        self.execution_time_str = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
        self.thread_tracking_file_path = os.path.join(self.hunt_log_path, self.THREAD_TRACKING_LOG)
        os.makedirs(self.hunt_log_path, exist_ok=True)

    def run(self):
        """
        Start the scheduler and process hunts.

        .. deprecated::
            Use ``HuntEngine.start()`` instead. The previous implementation used
            ``os.fork()`` daemonization which was fragile and Linux-only.
            This method now runs the asyncio loop directly (no fork).
        """
        import warnings

        warnings.warn(
            "CronRunner.run() is deprecated. Use HuntEngine.start() for background scheduling.",
            DeprecationWarning,
            stacklevel=2,
        )

        self.daemon_pid = os.getpid()
        logger.info(f"CronRunner started with PID: [{self.daemon_pid}]")

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(self.run_async())
        except Exception:
            logger.exception("Unhandled exception occurred in the CronRunner:", exc_info=True)
        finally:
            loop.close()

    async def run_async(self):
        rules_env = self.setup_environment()
        await self.setup_cron_runner(rules_env)
        await self.process_jobs()

    def setup_environment(self):
        """Set up Jinja2 environment for rule processing."""
        try:
            return Environment(loader=FileSystemLoader(self.rule_repo_dir), autoescape=True)
        except Exception:
            logger.exception("Failed to setup the Jinja2 environment.")
            raise

    async def setup_cron_runner(self, rules_env):
        """Set up and start the cron job runner if not already started."""
        try:
            if self.cron_runner is None:
                with self.scheduler_lock:
                    if self.cron_runner is None:
                        self.cron_runner = CronJob(
                            hunt_log_path=self.hunt_log_path,
                            target_config_data=self.target_config_data,
                            checkpoint_timestamp_field=self.checkpoint_timestamp_field,
                        )
                        await self.cron_runner.add_hunts_from_directory(
                            self.hunt_dir,
                            rules_env,
                            self.rule_repo_dir,
                            self.hunt_checkpoint_path,
                            self.checkpoint_destination,
                            self.thread_tracking_file_path,
                        )
                        await self.cron_runner.start_scheduler()
        except Exception as e:
            logger.error(
                "Failed to setup or start the cron runner for scheduler.",
                exc_info=True,
            )
            raise e

    async def process_jobs(self):
        """Process scheduled jobs until the timeout."""
        start_time = time.time()
        scheduler_stopped = False  # Flag to track if scheduler has been stopped
        try:
            while True:
                if self.hunt_cron_task_timeout and self.hunt_cron_task_timeout > 0:
                    elapsed_time = time.time() - start_time

                if (
                    self.hunt_cron_task_timeout
                    and self.hunt_cron_task_timeout > 0
                    and elapsed_time >= self.hunt_cron_task_timeout
                ):
                    logger.info(f"Timeout reached, stopping scheduler. PID: [{self.daemon_pid}]")
                    if not scheduler_stopped:
                        await self.stop_cron_runner()
                        scheduler_stopped = True
                    break
                await asyncio.sleep(0.10)
        except asyncio.CancelledError:
            logger.warning("Cron runner operation cancelled. PID: [{self.daemon_pid}]")
            if not scheduler_stopped:
                await self.stop_cron_runner()
        except Exception:
            logger.error(
                "Unexpected error during job processing. PID: [{self.daemon_pid}]",
                exc_info=True,
            )
            if not scheduler_stopped:
                await self.stop_cron_runner()
            raise

    async def stop_cron_runner(self):
        """Stop the cron runner if it's running."""
        try:
            if self.cron_runner:
                await self.cron_runner.stop_scheduler()
                logger.info(f"Scheduler stopped successfully. PID: [{self.daemon_pid}]")
                for hunt in self.cron_runner.hunts:
                    logger.info(f"Cron Runner Hunt - [{hunt.description}] PID: [{self.daemon_pid}]")
            else:
                logger.warning(f"Scheduler is not running. PID: [{self.daemon_pid}]")
        except Exception as e:
            logger.exception(
                f"Failed to stop the scheduler. PID: [{self.daemon_pid}]", exc_info=True
            )
            raise e
