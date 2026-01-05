import asyncio
import uuid
from typing import List, Callable
from concurrent.futures import ThreadPoolExecutor
from functools import partial

import os
from datetime import datetime, timezone
from jinja2 import Environment
import threading
from ..jobs.jobs import JobScheduler
from ..hunts.hunts import Hunt
from ..hunts.hunts_validator import HuntValidator
import croniter
from hs_pylib.logger import logger
from ...yaml_utils import yaml_load, YAMLError


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
    ):
        """
        Initializes the CronJob with a JobScheduler and sets up logging.
        """
        self.scheduler = JobScheduler()
        self.checkpoint_timestamp_field = checkpoint_timestamp_field
        self.target_config_data = target_config_data
        self.hunt_log_path = hunt_log_path
        self.hunts = []
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

        total_customers = len(customers)
        customer_counter = 0
        tasks = []

        for cron_expression in cron_expressions:
            hunt_frequency_minutes = self._get_hunt_frequency(cron_expression)
            if hunt_frequency_minutes is None:
                continue

            self._calculate_stagger_interval(hunt_frequency_minutes, total_customers)

            for customer in customers:
                await self._schedule_customer_hunt(
                    hunt_data=hunt_data,
                    env=env,
                    customer=customer,
                    cron_expression=cron_expression,
                    hunt_frequency_minutes=hunt_frequency_minutes,
                    customer_counter=customer_counter,
                    total_customers=total_customers,
                    actual_checkpoint_timestamp_field=actual_checkpoint_timestamp_field,
                    hunt_checkpoint_path=hunt_checkpoint_path,
                    checkpoint_destination=checkpoint_destination,
                    hunt_directory=hunt_directory,
                    thread_tracking_file_path=thread_tracking_file_path,
                )
                customer_counter += 1

        if tasks:
            await asyncio.gather(*tasks)

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
        cron_expression: str,
        hunt_frequency_minutes: float,
        customer_counter: int,
        total_customers: int,
        actual_checkpoint_timestamp_field: str,
        hunt_checkpoint_path: str,
        checkpoint_destination: str,
        hunt_directory: str,
        thread_tracking_file_path: str,
    ) -> None:
        """Schedule a hunt for a specific customer."""
        logger.debug(
            f"Scheduling hunts for customer: {customer} with cron: {cron_expression} and checkpoint field: {actual_checkpoint_timestamp_field}"
        )

        thread_id = threading.get_native_id()
        thread_id_customer = f"{customer}_{str(int(uuid.uuid4().hex, 16))[:12]}_{thread_id}"
        self.log_thread_details_for_all_hunts(
            thread_id=thread_id_customer,
            hunt_dir=hunt_directory,
            thread_tracking_file_path=thread_tracking_file_path,
        )

        minute_offset = int((customer_counter * self.stagger_minutes) % hunt_frequency_minutes)
        staggered_cron = self.modify_cron_expression(
            cron_expression, minute_offset, total_customers
        )
        logger.debug(
            f"Original cron expression: {cron_expression}, staggered cron expression: {staggered_cron}"
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
        )

        hunt.build_sql_queries_for_customers(env)
        job_func = partial(self._run_hunt_for_customer, hunt, customer)
        await self.add_cron_job(
            job_func, staggered_cron, f"{hunt.name}-{customer}-{staggered_cron}"
        )
        self.hunts.append(hunt)
        logger.debug(f"Total hunts: {len(self.hunts)} - {hunt.description}")
        logger.debug(f"Hunt Config: {hunt}")

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

    async def _run_hunt_for_customer(self, hunt: Hunt, customer: str) -> str:
        """
        Executes a hunt for a specific customer in a separate thread using ThreadPoolExecutor.

        :param hunt: The Hunt instance to be executed.
        :param customer: The customer data for whom the hunt is being executed.
        :return: The result of the hunt execution.
        """
        try:
            scheduled_start_time = datetime.now(timezone.utc)
            result = await asyncio.to_thread(hunt.execute_hunt, customer, scheduled_start_time)
            return result
        except Exception as e:
            logger.error(f"Error executing hunt for customer {customer}: {e}", exc_info=True)
            raise

    async def add_cron_job(
        self, job_function: Callable, cron_expression: str, job_name: str
    ) -> None:
        """
        Adds a cron job to the scheduler.

        :param job_function: The function to execute for the job.
        :param cron_expression: A cron expression that defines the job schedule.
        :param job_name: The name of the job.
        """
        try:
            await self.scheduler.add_job_with_cron(job_function, cron_expression, job_name)
            logger.debug(f"get_scheduled_jobs - [{self.get_scheduled_jobs()}]")
            logger.debug(
                f"describe_all_job_functions - [{self.scheduler.describe_all_job_functions()}]"
            )
        except Exception as e:
            logger.error(f"Error adding cron job: {e}", exc_info=True)

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
