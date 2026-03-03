import asyncio
import uuid
from typing import List, Callable, Tuple
from concurrent.futures import ThreadPoolExecutor
from functools import partial

import os
from datetime import datetime, timezone
from jinja2 import Environment
import threading
from .job import JobScheduler
from .hunt import Hunt
from .validator import HuntValidator
import croniter
from hyperi_pylib.logger import logger
from ..yaml_utils import yaml_load, YAMLError


def compute_stagger_offsets(
    cron_expression: str,
    total_jobs: int,
    jitter_seconds: int = 15,
) -> List[Tuple[str, int]]:
    """
    Compute evenly-distributed staggered cron expressions for N jobs.

    For N jobs with frequency F minutes: offset_i = (F / N) * i.
    Each job gets a modified cron expression with its offset baked in,
    plus a jitter value for sub-minute randomization.

    Args:
        cron_expression: Base cron expression (5-field).
        total_jobs: Number of jobs to distribute.
        jitter_seconds: Max random jitter in seconds per job.

    Returns:
        List of (modified_cron_expression, jitter_seconds) tuples.
    """
    if total_jobs <= 0:
        return []
    if total_jobs == 1:
        return [(cron_expression, jitter_seconds)]

    parts = cron_expression.split()
    if len(parts) != 5:
        return [(cron_expression, jitter_seconds)] * total_jobs

    # Per-minute cron: no stagger possible, jitter only
    if parts[0] == "*" and parts[1] == "*":
        return [(cron_expression, jitter_seconds)] * total_jobs

    # Calculate frequency in minutes using croniter
    frequency_minutes = _calculate_frequency(cron_expression)
    if frequency_minutes <= 0:
        return [(cron_expression, jitter_seconds)] * total_jobs

    results = []
    for i in range(total_jobs):
        offset_minutes = (frequency_minutes / total_jobs) * i
        modified_cron = _apply_offset(parts[:], offset_minutes, frequency_minutes)
        results.append((modified_cron, jitter_seconds))

    return results


def _calculate_frequency(cron_expression: str) -> float:
    """Calculate frequency in minutes from a cron expression using croniter."""
    try:
        base = datetime(2025, 1, 1)
        cron_iter = croniter.croniter(cron_expression, base)
        num_intervals = 24
        total_seconds = 0
        previous_time = base

        for _ in range(num_intervals):
            next_time = cron_iter.get_next(datetime)
            total_seconds += (next_time - previous_time).total_seconds()
            previous_time = next_time

        return total_seconds / num_intervals / 60
    except Exception:
        return 0


def _apply_offset(parts: list, offset_minutes: float, frequency_minutes: float) -> str:
    """Apply a minute/hour offset to a cron expression."""
    # Step-minute cron: */N * * * *
    if "*/" in parts[0]:
        step = int(parts[0].split("/")[1])
        minute_offset = int(offset_minutes) % step
        parts[0] = f"{minute_offset}/{step}"
        return " ".join(parts)

    # Fixed-minute cron with hour step: M */H * * *  or  M H * * *
    if parts[1] != "*" and "*/" in parts[1]:
        hour_step = int(parts[1].split("/")[1])
        total_interval_minutes = hour_step * 60
        total_offset = int(offset_minutes) % total_interval_minutes
        hour_offset = total_offset // 60
        minute_value = total_offset % 60
        parts[0] = str(minute_value)
        parts[1] = f"{hour_offset}/{hour_step}"
        return " ".join(parts)

    if offset_minutes == 0:
        return " ".join(parts)

    # Fixed minute(s): e.g. "0,30 * * * *" or "15 * * * *"
    try:
        minutes = [int(m) for m in parts[0].split(",")]
        offset = int(offset_minutes)
        new_minutes = sorted(set((m + offset) % 60 for m in minutes))
        parts[0] = ",".join(str(m) for m in new_minutes)
        return " ".join(parts)
    except ValueError:
        return " ".join(parts)


class CronJob:
    """
    A class to manage the execution of scheduled hunts.
    """

    def __init__(
        self,
        hunt_log_path: str,
        target_config_data: dict,
        checkpoint_timestamp_field: str,
        max_workers=None,
        jitter_seconds: int = 15,
        scheduling_mode: str = "cron",
        min_interval_seconds: int = 0,
        explain_queries: bool = False,
        source_registry=None,
        concurrency_semaphore=None,
        resource_limits: dict | None = None,
    ):
        """
        Initializes the CronJob with a JobScheduler and sets up logging.
        """
        self.scheduler = JobScheduler()
        self.checkpoint_timestamp_field = checkpoint_timestamp_field
        self.target_config_data = target_config_data
        self.hunt_log_path = hunt_log_path
        self.hunts = []
        self._jitter_seconds = jitter_seconds
        self._scheduling_mode = scheduling_mode
        self._min_interval_seconds = min_interval_seconds
        self._explain_queries = explain_queries
        self._source_registry = source_registry
        self._concurrency_semaphore = concurrency_semaphore
        self._resource_limits = resource_limits or {}
        logger.info("cron job dfe logger initialized")
        self.scheduled_start_time = datetime.now(timezone.utc)
        self.executor = ThreadPoolExecutor(max_workers=max_workers)
        self.stagger_minutes = 0

    def log_thread_details_for_all_hunts(self, thread_id, hunt_dir, thread_tracking_file_path):
        """
        Logs the details of the current thread, associated with all hunts in the hunt directory, and timestamp to a tracking file.
        """
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        hunt_files = [
            file for file in os.listdir(hunt_dir) if os.path.isfile(os.path.join(hunt_dir, file))
        ]
        for hunt_file in hunt_files:
            if hunt_file.endswith((".yml", ".yaml")):  # Filter for .yml and .yaml files
                log_entry = (
                    f"thread_id={thread_id},pid={os.getpid()},hunt_file={hunt_file},"
                    f"hunt_name={hunt_file.split('.')[0]},timestamp={timestamp},hunt_source={hunt_dir}\n"
                )
                with open(thread_tracking_file_path, "a") as file:
                    file.write(log_entry)
            else:
                logger.warning(f"Hunt Directory contains this illegal file: [{hunt_file}].")

        logger.debug(f"Logged thread details for all relevant hunts in directory: {hunt_dir}.")

    async def add_hunts_from_directory(
        self,
        hunt_directory: str,
        env: Environment,
        rule_repo_dir: str,
        hunt_checkpoint_path: str,
        checkpoint_destination: str,
        thread_tracking_file_path: str,
    ) -> None:
        """
        Adds hunts from a directory containing YAML files that define hunts.

        :param hunt_directory: The directory containing hunt YAML files.
        :param env: A Jinja2 Environment instance for SQL template rendering.
        :param rule_repo_dir: The directory where rule templates are stored.
        :param hunt_checkpoint_path: The path to store hunt checkpoints.
        :param checkpoint_destination: The destination type for checkpoints.
        """
        number_of_hunts = 0
        logger.info(f"Current working directory is {os.getcwd()} - {hunt_directory}")

        for filename in os.listdir(hunt_directory):
            logger.info(f"Processing hunts [{hunt_directory}] - [{filename}] - [{number_of_hunts}]")

            if filename.endswith(".yml") or filename.endswith(".yaml"):
                file_path = os.path.join(hunt_directory, filename)
                try:
                    hunt_data = yaml_load(file_path)
                    HuntValidator.validate_hunt_configuration(
                        hunt_data,
                        env,
                        rule_repo_dir,
                        self.checkpoint_timestamp_field,
                    )
                    await self.process_hunt(
                        hunt_data,
                        env,
                        hunt_checkpoint_path,
                        checkpoint_destination,
                        hunt_directory,
                        thread_tracking_file_path,
                    )
                    number_of_hunts += 1
                except ValueError as ve:
                    logger.error(f"Validation error in file {filename}: {ve}")
                except YAMLError as ye:
                    logger.error(f"Error parsing YAML file {filename}: {ye}")

        logger.info(f"Added [{number_of_hunts}]")

    async def process_hunt(
        self,
        hunt_data: dict,
        env: Environment,
        hunt_checkpoint_path: str,
        checkpoint_destination: str,
        hunt_directory: str,
        thread_tracking_file_path: str,
    ):
        """
        Processes hunt data, creating a Hunt instance and scheduling it for multiple cron expressions and customers.

        :param hunt_data: A dictionary containing hunt configuration.
        :param env: A Jinja2 Environment instance for SQL template rendering.
        :param hunt_checkpoint_path: Path for hunt checkpoints.
        :param checkpoint_destination: Destination for checkpoints.
        :param hunt_directory: Directory containing hunt configurations.
        :param thread_tracking_file_path: Path for thread tracking file.
        """
        logger.debug(f"Creating hunt with this data: \n {hunt_data}")

        customers = hunt_data.get("customers", [])
        cron_expressions = self._parse_cron_config(hunt_data.get("cron", []))
        actual_checkpoint_timestamp_field = self._get_checkpoint_field(hunt_data)

        # Per-hunt overrides (fall back to CronJob-level defaults)
        hunt_scheduling_mode = hunt_data.get("scheduling_mode", self._scheduling_mode)
        hunt_min_interval = hunt_data.get("min_interval_seconds", self._min_interval_seconds)
        hunt_explain = hunt_data.get("explain_queries", self._explain_queries)

        for cron_expression in cron_expressions:
            # Compute evenly-distributed stagger offsets for all customers
            offsets = compute_stagger_offsets(
                cron_expression,
                total_jobs=len(customers),
                jitter_seconds=self._jitter_seconds,
            )

            for i, customer in enumerate(customers):
                staggered_cron, jitter = offsets[i]
                await self._schedule_customer_hunt(
                    hunt_data=hunt_data,
                    env=env,
                    customer=customer,
                    staggered_cron=staggered_cron,
                    jitter=jitter,
                    actual_checkpoint_timestamp_field=actual_checkpoint_timestamp_field,
                    hunt_checkpoint_path=hunt_checkpoint_path,
                    checkpoint_destination=checkpoint_destination,
                    hunt_directory=hunt_directory,
                    thread_tracking_file_path=thread_tracking_file_path,
                    scheduling_mode=hunt_scheduling_mode,
                    min_interval_seconds=hunt_min_interval,
                    explain_queries=hunt_explain,
                )

    def _parse_cron_config(self, cron_config) -> list:
        """Parse cron configuration into a list of expressions."""
        if isinstance(cron_config, str):
            if "," in cron_config:
                error_msg = (
                    f"Comma-separated cron expressions are not supported: '{cron_config}'. "
                    f'Please use a list format instead:\ncron:\n  - "{cron_config.split(",")[0]}"\n  - "{cron_config.split(",")[1]}"'
                )
                logger.error(error_msg)
                raise ValueError(error_msg)
            logger.info(f"Using single cron expression: {cron_config}")
            return [cron_config]
        elif isinstance(cron_config, list):
            logger.info(f"Using multiple cron expressions: {cron_config}")
            return cron_config
        else:
            error_msg = f"Invalid cron format: {cron_config}. Must be a string or list."
            logger.error(error_msg)
            raise ValueError(error_msg)

    def _get_checkpoint_field(self, hunt_data: dict) -> str:
        """Get the checkpoint timestamp field from hunt data or defaults."""
        if not self.checkpoint_timestamp_field:
            self.checkpoint_timestamp_field = "timestamp_load"
        return hunt_data.get("checkpoint_timestamp_field", self.checkpoint_timestamp_field)

    def _get_hunt_frequency(self, cron_expression: str) -> float | None:
        """Calculate hunt frequency from cron expression, returning None if invalid."""
        logger.info(f"Determining the frequency of hunt with cron: {cron_expression}")

        try:
            hunt_frequency_minutes = self.calculate_frequency_from_cron(cron_expression)
            logger.info(
                f"Hunt Frequency from cron {cron_expression}: {hunt_frequency_minutes} minutes"
            )
            if hunt_frequency_minutes <= 0:
                logger.warning(
                    f"Invalid hunt frequency ({hunt_frequency_minutes} minutes) from cron {cron_expression}. Skipping."
                )
                return None
            return hunt_frequency_minutes
        except ValueError as e:
            logger.error(f"Invalid cron expression {cron_expression}: {e}. Skipping.")
            return None

    def _calculate_stagger_interval(
        self, hunt_frequency_minutes: float, total_customers: int
    ) -> None:
        """Calculate the stagger interval for distributing customer hunts."""
        min_intervals = max(total_customers, 10)
        self.stagger_minutes = hunt_frequency_minutes / min_intervals
        self.stagger_minutes = max(1, round(self.stagger_minutes))
        logger.debug(
            f"Calculated stagger interval: {self.stagger_minutes} minutes across {min_intervals} intervals"
        )

    async def _schedule_customer_hunt(
        self,
        hunt_data: dict,
        env: Environment,
        customer: str,
        staggered_cron: str,
        jitter: int,
        actual_checkpoint_timestamp_field: str,
        hunt_checkpoint_path: str,
        checkpoint_destination: str,
        hunt_directory: str,
        thread_tracking_file_path: str,
        scheduling_mode: str = "cron",
        min_interval_seconds: int = 0,
        explain_queries: bool = False,
    ) -> None:
        """Schedule a hunt for a specific customer with even load spreading."""
        logger.debug(
            f"Scheduling hunt for customer: {customer} with cron: {staggered_cron} "
            f"(jitter: {jitter}s), checkpoint field: {actual_checkpoint_timestamp_field}"
        )

        thread_id = threading.get_native_id()
        thread_id_customer = f"{customer}_{str(int(uuid.uuid4().hex, 16))[:12]}_{thread_id}"
        self.log_thread_details_for_all_hunts(
            thread_id=thread_id_customer,
            hunt_dir=hunt_directory,
            thread_tracking_file_path=thread_tracking_file_path,
        )

        hunt = Hunt(
            cron=staggered_cron,
            log_buffer=hunt_data["log_buffer"],
            customer=customer,
            rules=hunt_data["rules"],
            name=hunt_data["name"],
            global_source_table_name=hunt_data["global_source_table_name"],
            global_target_table_name=hunt_data["global_target_table_name"],
            hunt_log_path=self.hunt_log_path,
            target_config_data=self.target_config_data,
            checkpoint_timestamp_field=actual_checkpoint_timestamp_field,
            customer_filters=hunt_data.get("customer_filters", {}),
            checkpoint_destination=checkpoint_destination,
            hunt_checkpoint_path=hunt_checkpoint_path,
            thread_id=thread_id_customer,
            explain_queries=explain_queries,
            source_registry=self._source_registry,
            resource_limits=self._resource_limits,
        )

        hunt.build_sql_queries_for_customers(env)
        job_func = partial(self._run_hunt_for_customer, hunt, customer)
        job_name = f"{hunt.name}-{customer}-{staggered_cron}"
        job = await self.add_cron_job(job_func, staggered_cron, job_name, jitter=jitter)

        # Enable adaptive REFRESH AFTER scheduling
        if scheduling_mode == "adaptive" and job is not None:
            interval = min_interval_seconds
            if interval <= 0:
                freq_minutes = self._get_hunt_frequency(staggered_cron)
                interval = int((freq_minutes or 5) * 60)
            self.scheduler.enable_adaptive_mode(job.id, interval)
            logger.info(
                f"Adaptive scheduling for {job_name}: interval={interval}s after completion"
            )

        self.hunts.append(hunt)
        logger.debug(f"Total hunts: {len(self.hunts)} - {hunt.description}")

    def calculate_frequency_from_cron(self, cron_expression: str) -> float:
        """
        Calculates the frequency in minutes from a cron expression.

        This function uses the croniter library to determine the average time between executions
        over a 24-hour period.

        :param cron_expression: The cron expression to analyze.
        :return: The estimated frequency in minutes, or None if the cron is invalid.
        """
        try:
            base = datetime.now()
            iter = croniter.croniter(cron_expression, base)
            num_intervals = 24
            total_seconds = 0
            previous_time = base

            for _ in range(num_intervals):
                next_time = iter.get_next(datetime)
                interval = next_time - previous_time
                total_seconds += interval.total_seconds()
                previous_time = next_time

            average_interval_seconds = total_seconds / num_intervals
            frequency_minutes = average_interval_seconds / 60

            return frequency_minutes
        except Exception as e:
            raise ValueError(f"Invalid cron expression: {e}") from e

    def modify_cron_expression(
        self, cron_expression: str, minute_offset: int, total_customers: int
    ) -> str:
        """
        Modifies a cron expression to add a minute and hour offset for staggering.

        :param cron_expression: The original cron expression.
        :param minute_offset: The number of minutes to offset the cron schedule.
        :return: The modified cron expression.
        """
        parts = cron_expression.split()
        if len(parts) != 5:
            logger.warning(f"Invalid cron expression: {cron_expression}. Using original.")
            return cron_expression

        minute_offset = int(minute_offset)

        if parts[0] == "*" or (parts[0].count(",") > 30):
            parts[0] = "*"
            logger.info(f"Modified for per-minute execution with customer {minute_offset}")
            return " ".join(parts)

        if parts[0] == "0" and parts[1].startswith("*/"):
            hour_step = int(parts[1].split("/")[1])
            if hour_step >= 2:
                total_minutes_in_interval = hour_step * 60
                customer_index = minute_offset // self.stagger_minutes
                total_offset_minutes = (
                    customer_index * total_minutes_in_interval
                ) // total_customers
                hour_offset = total_offset_minutes // 60
                minute_value = total_offset_minutes % 60
                hour_offset = hour_offset % 24

                if hour_offset + hour_step > 23:
                    hour_step = 24 - hour_offset

                parts[0] = str(minute_value)
                parts[1] = f"{hour_offset}/{hour_step}"
                logger.info(
                    f"Staggered long-interval cron: original={cron_expression}, modified={' '.join(parts)}, "
                    f"will run at minute {minute_value} of hours {hour_offset}, {hour_offset + hour_step}, "
                    f"{hour_offset + (2 * hour_step)}, etc."
                )
                return " ".join(parts)

        if "*/" in parts[0]:
            step = int(parts[0].split("/")[1])
            offset = minute_offset % step
            parts[0] = f"{offset}/{step}"
            return " ".join(parts)

        try:
            minutes = parts[0].split(",")
            new_minutes = [(int(m) + minute_offset) % 60 for m in minutes]
            parts[0] = ",".join(map(str, sorted(new_minutes)))
        except ValueError:
            logger.warning(f"Cannot modify minute field: {parts[0]}. Using original.")
            return cron_expression

        return " ".join(parts)

    async def _run_hunt_for_customer(self, hunt: Hunt, customer: str) -> dict:
        """
        Executes a hunt for a specific customer in a separate thread.

        Applies concurrency semaphore if configured. Tracks execution lag
        (backpressure signal) comparing execution duration to cron interval.
        """
        try:
            scheduled_start_time = datetime.now(timezone.utc)

            if self._concurrency_semaphore is not None:
                async with self._concurrency_semaphore:
                    result = await asyncio.to_thread(
                        hunt.execute_hunt, customer, scheduled_start_time
                    )
            else:
                result = await asyncio.to_thread(
                    hunt.execute_hunt, customer, scheduled_start_time
                )

            # Backpressure signal: compare execution duration to cron interval
            execution_seconds = result.get("total_execution_time", 0)
            interval_seconds = self._get_hunt_interval_seconds(hunt.cron)
            if interval_seconds and execution_seconds > interval_seconds:
                lag_seconds = execution_seconds - interval_seconds
                logger.warning(
                    f"BACKPRESSURE: hunt '{hunt.name}' for '{customer}' "
                    f"took {execution_seconds:.1f}s but interval is {interval_seconds}s "
                    f"(lag={lag_seconds:.1f}s). Hunt is falling behind."
                )
                result["lag_seconds"] = lag_seconds
                result["is_lagging"] = True
            else:
                result["lag_seconds"] = 0
                result["is_lagging"] = False

            return result
        except Exception as e:
            logger.error(f"Error executing hunt for customer {customer}: {e}", exc_info=True)
            raise

    def _get_hunt_interval_seconds(self, cron_expression: str) -> float | None:
        """Get the cron interval in seconds for backpressure calculation."""
        try:
            freq = self.calculate_frequency_from_cron(cron_expression)
            return freq * 60 if freq and freq > 0 else None
        except (ValueError, Exception):
            return None

    async def add_cron_job(
        self, job_function: Callable, cron_expression: str, job_name: str,
        jitter: int = 0,
    ):
        """
        Adds a cron job to the scheduler.

        :param job_function: The function to execute for the job.
        :param cron_expression: A cron expression that defines the job schedule.
        :param job_name: The name of the job.
        :param jitter: Max random delay in seconds added to each fire time.
        :return: The APScheduler job object, or None on error.
        """
        try:
            job = await self.scheduler.add_job_with_cron(
                job_function, cron_expression, job_name, jitter=jitter
            )
            logger.debug(f"get_scheduled_jobs - [{self.get_scheduled_jobs()}]")
            logger.debug(
                f"describe_all_job_functions - [{self.scheduler.describe_all_job_functions()}]"
            )
            return job
        except Exception as e:
            logger.error(f"Error adding cron job: {e}", exc_info=True)
            return None

    async def stop_scheduler(self):
        """
        Stops the scheduler and all scheduled jobs.
        """
        try:
            await self.scheduler.stop_all_jobs()
        except Exception as e:
            logger.error(f"Error stopping scheduler: {e}", exc_info=True)

    async def start_scheduler(self) -> None:
        """
        Starts the scheduler if it's not already running.
        """
        if self.scheduler is None:
            logger.warning("Scheduler is not initialized.")
            return
        if not self.hunts:
            logger.warning("No jobs to run. Scheduler not started.")
            return
        try:
            await self.scheduler.start_scheduler()
            logger.info("Scheduler started successfully with these hunts.")
            for hunt in self.hunts:
                logger.debug(f"Cron Job Hunt Description [{hunt.description}]")
        except Exception as e:
            logger.error(f"Error starting scheduler: {e}", exc_info=True)

    def get_scheduled_jobs(self) -> List[str]:
        """
        Returns a list of scheduled jobs.

        :return: A list of strings describing each scheduled job.
        """
        return self.scheduler.get_scheduled_jobs()

    def print_scheduled_jobs(self):
        """
        Logs the scheduled jobs.
        """
        if not self.scheduler.get_scheduled_jobs():
            logger.info("No scheduled jobs to display.")
            return None
        self.scheduler.print_scheduled_jobs()
