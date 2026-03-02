"""
HuntEngine — background thread scheduler for hunt execution.

Replaces the fragile HuntScheduler + CronRunner (os.fork()) architecture
with a single daemon thread running an asyncio event loop.

Usage:
    from dfe_engine.hunts import HuntEngine

    engine = HuntEngine()  # reads from get_settings()
    engine.start()         # non-blocking, returns when scheduler is ready
    ...
    engine.stop()          # graceful shutdown
"""

import asyncio
import os
import threading
from typing import Optional

from hyperi_pylib.logger import logger

from ..settings import DFESettings, get_settings, get_clickhouse_config


class HuntEngine:
    """
    Background thread manager for hunt scheduling.

    Lifecycle: start() -> is_running -> stop()

    Runs APScheduler on a daemon thread. Reads all configuration from
    DFESettings (no dfe_package.yaml dependency).
    """

    def __init__(self, settings: Optional[DFESettings] = None):
        self._settings = settings or get_settings()
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._started = threading.Event()
        self._cron_jobs: list = []
        self._lock = threading.Lock()

    @property
    def is_running(self) -> bool:
        """Whether the background scheduler thread is alive."""
        return self._thread is not None and self._thread.is_alive()

    def start(self, timeout: float = 30.0) -> None:
        """
        Start the hunt engine on a background daemon thread.

        Blocks until the scheduler is ready or timeout is reached.

        Args:
            timeout: Max seconds to wait for scheduler to become ready.
        """
        with self._lock:
            if self.is_running:
                logger.warning("HuntEngine is already running.")
                return

            self._started.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="dfe-hunt-engine",
                daemon=True,
            )
            self._thread.start()

        if not self._started.wait(timeout=timeout):
            logger.warning("HuntEngine start timed out waiting for scheduler readiness.")

        logger.info("HuntEngine started.")

    def stop(self, timeout: float = 10.0) -> None:
        """
        Stop the hunt engine gracefully.

        Args:
            timeout: Max seconds to wait for shutdown.
        """
        with self._lock:
            if not self.is_running:
                return

            if self._loop and self._stop_event:
                self._loop.call_soon_threadsafe(self._stop_event.set)

            self._thread.join(timeout=timeout)
            self._thread = None
            logger.info("HuntEngine stopped.")

    def _run(self) -> None:
        """Thread target: create event loop, set up CronJobs, run until stop."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._run_async())
        except Exception:
            logger.exception("HuntEngine crashed.")
        finally:
            self._loop.close()
            self._loop = None

    async def _run_async(self) -> None:
        """Async main: set up hunt directories, start schedulers, wait for stop."""
        self._stop_event = asyncio.Event()
        hunts_cfg = self._settings.hunts
        target_config = get_clickhouse_config(self._settings)

        hunt_dirs = self._parse_dirs(hunts_cfg.hunt_dir)
        rule_dirs = self._parse_dirs(hunts_cfg.rule_repo_dir)

        if not hunt_dirs:
            logger.warning("HuntEngine: no hunt_dir configured. Nothing to schedule.")
            self._started.set()
            return

        if len(hunt_dirs) != len(rule_dirs):
            raise ValueError(
                f"hunt_dir count ({len(hunt_dirs)}) != rule_repo_dir count ({len(rule_dirs)}). "
                "Ensure each hunt directory is paired with a rule directory."
            )

        for hunt_dir, rule_dir in zip(hunt_dirs, rule_dirs, strict=True):
            if not os.path.isdir(hunt_dir):
                logger.error(f"Hunt directory not found: {hunt_dir}")
                continue
            if not os.path.isdir(rule_dir):
                logger.error(f"Rule directory not found: {rule_dir}")
                continue

            cron_job = await self._setup_cron_job(
                hunt_dir, rule_dir, hunts_cfg, target_config
            )
            if cron_job:
                self._cron_jobs.append(cron_job)

        self._started.set()

        if not self._cron_jobs:
            logger.warning("HuntEngine: no hunts loaded. Exiting.")
            return

        # Wait for stop signal or timeout
        if hunts_cfg.cron_task_timeout > 0:
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=hunts_cfg.cron_task_timeout,
                )
            except asyncio.TimeoutError:
                logger.info(
                    f"HuntEngine: timeout reached ({hunts_cfg.cron_task_timeout}s)."
                )
        else:
            await self._stop_event.wait()

        # Graceful shutdown of all CronJobs
        for cron_job in self._cron_jobs:
            try:
                await cron_job.stop_scheduler()
            except Exception:
                logger.exception("Error stopping CronJob scheduler.")

        self._cron_jobs.clear()

    async def _setup_cron_job(
        self, hunt_dir: str, rule_dir: str, hunts_cfg, target_config: dict
    ):
        """Set up a CronJob for a single hunt_dir/rule_dir pair."""
        from jinja2 import Environment, FileSystemLoader

        from .cron_job import CronJob

        try:
            rules_env = Environment(
                loader=FileSystemLoader(rule_dir), autoescape=True
            )
            cron_job = CronJob(
                hunt_log_path=hunts_cfg.log_path,
                target_config_data=target_config,
                checkpoint_timestamp_field=hunts_cfg.checkpoint_timestamp_field,
                jitter_seconds=hunts_cfg.jitter_seconds,
            )

            thread_tracking_path = os.path.join(
                hunts_cfg.log_path, "thread_tracking.log"
            )
            os.makedirs(hunts_cfg.log_path, exist_ok=True)

            await cron_job.add_hunts_from_directory(
                hunt_dir,
                rules_env,
                rule_dir,
                hunts_cfg.checkpoint_path,
                hunts_cfg.checkpoint_destination,
                thread_tracking_path,
            )
            await cron_job.start_scheduler()

            logger.info(
                f"HuntEngine: started scheduler for {hunt_dir} "
                f"with {len(cron_job.hunts)} hunts."
            )
            return cron_job
        except Exception:
            logger.exception(
                f"HuntEngine: failed to set up scheduler for {hunt_dir}."
            )
            return None

    @staticmethod
    def _parse_dirs(dirs_str: str) -> list:
        """Parse comma-separated directory string into a list."""
        if not dirs_str:
            return []
        return [d.strip() for d in dirs_str.split(",") if d.strip()]
