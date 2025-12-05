import asyncio
import time
import threading
import os
import sys

from datetime import datetime, timezone
from jinja2 import Environment, FileSystemLoader
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
        logger: logging.Logger,
        hunt_log_path: str,
        checkpoint_timestamp_field: str,
        target_config_data: dict,
    ):
        self.hunt_dir = hunt_dir
        self.rule_repo_dir = rule_repo_dir
        self.hunt_checkpoint_path = hunt_checkpoint_path
        self.hunt_cron_task_timeout = hunt_cron_task_timeout
        self.checkpoint_destination = checkpoint_destination
        self.target_config_data = target_config_data
        self.logger = logger
        self.checkpoint_timestamp_field = checkpoint_timestamp_field
        self.cron_runner = None
        self.daemon_pid = None
        self.scheduler_lock = threading.Lock()
        self.hunt_log_path = hunt_log_path or os.getenv(
            "HUNT_LOG_PATH", CronRunner.DEFAULT_HUNT_LOG_FILE_PATH
        )
        self.execution_time_str = datetime.now(timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        self.thread_tracking_file_path = os.path.join(
            self.hunt_log_path, self.THREAD_TRACKING_LOG
        )
        os.makedirs(self.hunt_log_path, exist_ok=True)

    def run(self):
        """
        Main function to start the scheduler and process hunts.

        This method daemonizes the process, sets up logging, and starts an asyncio event loop.
        The steps are as follows:

        1. First Fork:
            - The process is forked to create a child process.
            - If the current process is the parent (`pid > 0`), it exits.

        2. Create a New Session:
            - The child process becomes the session leader of a new session and the process group leader of a new process group.
            - This detaches the process from any controlling terminal.

        3. Second Fork:
            - The process is forked again to ensure it is not a session leader, preventing it from acquiring a controlling terminal.
            - The parent of this fork exits, leaving the grandchild process running.

        4. Redirect Standard File Descriptors:
            - Standard output and error are flushed to ensure no buffered data is lost.
            - Standard input is redirected to `/pre_build_config/null`, effectively ignoring any input.
            - Standard output and error are redirected to 'hunt_cli_logs.log'.
            - This setup ensures that the daemon process does not output to the terminal but logs to the specified file.

        5. Start the Asyncio Event Loop:
            - A new asyncio event loop is created and set as the current event loop.
            - The `self.run_async()` coroutine is run until complete within the event loop.
            - The event loop is closed after completion to clean up resources.
        """
        self.logger.info(" forking cron runner onto a thread target")

        pid = os.fork()
        if pid > 0:
            sys.exit(0)
        os.setsid()

        pid = os.fork()
        if pid > 0:
            sys.exit(0)

        self.daemon_pid = os.getpid()
        self.logger.info(f"Daemon process started with PID: [{self.daemon_pid}]")

        asyncio.set_event_loop(asyncio.new_event_loop())
        loop = asyncio.get_event_loop()
        try:
            return loop.run_until_complete(self.run_async())
        except Exception:
            self.logger.exception(
                "Unhandled exception occurred in the CronRunner:", exc_info=True
            )
        finally:
            loop.close()

    async def run_async(self):
        rules_env = self.setup_environment()
        await self.setup_cron_runner(rules_env)
        await self.process_jobs()

    def setup_environment(self):
        """Set up Jinja2 environment for rule processing."""
        try:
            return Environment(loader=FileSystemLoader(self.rule_repo_dir))
        except Exception:
            self.logger.exception("Failed to setup the Jinja2 environment.")
            raise

    async def setup_cron_runner(self, rules_env):
        """Set up and start the cron job runner if not already started."""
        try:
            if self.cron_runner is None:
                with self.scheduler_lock:
                    if self.cron_runner is None:
                        self.cron_runner = CronJob(
                            hunt_log_path=self.hunt_log_path,
                            logger=self.logger,
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
            self.logger.error(
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
                    self.hunt_cron_task_timeout - elapsed_time
                    # self.logger.info(f"Timeout set, this scheduler will stop in: {int(time_left)} seconds. PID: [{self.daemon_pid}]")

                if (
                    self.hunt_cron_task_timeout
                    and self.hunt_cron_task_timeout > 0
                    and elapsed_time >= self.hunt_cron_task_timeout
                ):
                    self.logger.info(
                        f"Timeout reached, stopping scheduler. PID: [{self.daemon_pid}]"
                    )
                    if not scheduler_stopped:
                        await self.stop_cron_runner()
                        scheduler_stopped = True
                    break
                await asyncio.sleep(0.10)
        except asyncio.CancelledError:
            self.logger.warning(
                "Cron runner operation cancelled. PID: [{self.daemon_pid}]"
            )
            if not scheduler_stopped:
                await self.stop_cron_runner()
        except Exception:
            self.logger.error(
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
                self.logger.info(
                    f"Scheduler stopped successfully. PID: [{self.daemon_pid}]"
                )
                for hunt in self.cron_runner.hunts:
                    self.logger.info(
                        f"Cron Runner Hunt - [{hunt.description}] PID: [{self.daemon_pid}]"
                    )
            else:
                self.logger.warning(
                    f"Scheduler is not running. PID: [{self.daemon_pid}]"
                )
        except Exception as e:
            self.logger.exception(
                f"Failed to stop the scheduler. PID: [{self.daemon_pid}]", exc_info=True
            )
            raise e
