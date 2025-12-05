from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.events import EVENT_JOB_EXECUTED, EVENT_JOB_ERROR
from apscheduler.triggers.interval import IntervalTrigger

from typing import Callable, Any, List, Optional, Dict
from datetime import datetime


class JobScheduler:
    def __init__(self, logger: logging.Logger = None):
        """
        Initialize the job scheduler with APScheduler and staggering support.

        The scheduler:
        1. Uses AsyncIOScheduler for async job execution
        2. Maintains list of active jobs for stagger calculation
        3. Provides job execution monitoring via listener
        """
        self.logger = logger
        self.scheduler = AsyncIOScheduler()
        self.active_jobs = []
        self.scheduler_started = False
        self.scheduled_start_time = None
        self.scheduler.add_listener(
            self.job_listener, EVENT_JOB_EXECUTED | EVENT_JOB_ERROR
        )

    def job_listener(self, event):
        """
        Monitor job execution and log results.

        Captures:
        1. Job completion status and timing
        2. Execution results and statistics
        3. Next scheduled run time
        """
        job = self.scheduler.get_job(event.job_id)
        self.scheduled_start_time = (
            event.scheduled_run_time if hasattr(event, "scheduled_run_time") else "N/A"
        )

        if event.exception:
            self.logger.error(
                f"The job {job.name} (ID: {job.id}) crashed: {event.exception}",
                exc_info=True,
            )
        else:
            completion_time = datetime.now()
            result = event.retval if hasattr(event, "retval") else "No result returned"
            next_run_time = job.next_run_time if job.next_run_time else None

            details = (
                f"The job {job.name} (ID: {job.id}) completed successfully. "
                f"Start time: {self.scheduled_start_time}, Completion time: {completion_time}, Next run time: {next_run_time}. "
            )

            if isinstance(result, dict):
                details += (
                    f"Total Execution Time: {result.get('total_execution_time')}, "
                    f"Successful Queries: {result.get('successful_queries')}, "
                    f"Failed Queries: {result.get('failed_queries')}, "
                    f"Hunt Name: {result.get('hunt_name')}. "
                )

            self.logger.debug(details)

    async def add_job_with_cron(
        self, job_function: Callable, cron_expression: str, job_name: str
    ) -> Any:
        """
        Add a job to the scheduler based on a cron expression.
        """
        if self.active_jobs is None:
            self.logger.warning("No active jobs list available. Skipping job addition.")
            return

        try:
            trigger = CronTrigger.from_crontab(cron_expression)
            job_id = f"{job_name}-{len(self.active_jobs)}"
            job = self.scheduler.add_job(
                job_function,
                trigger,
                id=job_id,
                name=job_name,
                misfire_grace_time=5,
                coalesce=True,
                max_instances=1,
                replace_existing=False,
            )
            self.active_jobs.append(job)
            self.logger.debug(
                f"Added cron job [{job_name}] with:\nSchedule: {cron_expression}"
            )
            return job
        except Exception as e:
            self.logger.error(f"Error adding job with cron job : {e}")
            raise

    async def add_job_with_interval(
        self, job_function: Callable, seconds: int, job_name: str
    ) -> Any:
        """
        Add a job to the scheduler to run at a regular interval in seconds.
        """
        if self.active_jobs is None:
            self.logger.warning("No active jobs list available. Skipping job addition.")
            return

        try:
            trigger = IntervalTrigger(seconds=seconds)
            job_id = f"{job_name}-interval-{len(self.active_jobs)}"
            job = self.scheduler.add_job(
                job_function,
                trigger,
                id=job_id,
                name=job_name,
                misfire_grace_time=5,
                coalesce=True,
                max_instances=1,
                replace_existing=False,
            )
            self.active_jobs.append(job)
            self.logger.info(
                f"Added interval job [{job_name}] with:\nInterval: {seconds} seconds"
            )
            return job
        except Exception as e:
            self.logger.error(f"Error adding job with interval: {e}")
            raise

    async def start_scheduler(self) -> Optional[bool]:
        """
        Start the scheduler with all configured jobs.

        The function:
        1. Checks scheduler and job status
        2. Starts scheduler if not running
        3. Logs all scheduled jobs and their next run times

        Returns:
            bool: True if started, False if no jobs or already running
            None: If an error occurs
        """
        if not self.scheduler.running and self.active_jobs:
            self.scheduler.start()
            self.scheduler_started = True
            for job in self.scheduler.get_jobs():
                self.logger.debug(
                    f"Scheduled Job: {job.name}, Next Run Time: {job.next_run_time}"
                )
            self.logger.info("Scheduler started.")
            return True
        elif not self.active_jobs:
            self.logger.warning("No jobs to run. Scheduler not started.")
            return False
        else:
            self.logger.warning("Scheduler is already running.")
            return False

    async def stop_all_jobs(self) -> List[str]:
        """
        Stop all running jobs and clean up.

        Returns:
            List of job details that were stopped
        """
        job_details = []
        for job in self.active_jobs:
            job_details.append(f"Job ID: {job.id}, Job Name: {job.name}")
            job.remove()
        self.active_jobs.clear()
        return job_details

    def get_scheduled_jobs(self) -> List[Any]:
        """
        Get a list of scheduled jobs with their details.

        Returns:
            List[str]: A list containing formatted details of each scheduled job.
        """
        return self.scheduler.get_jobs()

    def print_scheduled_jobs(self):
        """
        Print a formatted list of scheduled jobs to the console.
        """
        jobs = self.scheduler.get_jobs()
        if jobs:
            for job in jobs:
                job_info = self.get_job_info(job)
                self.logger.info(f"Job Info: {job_info}")
        else:
            self.logger.info("No scheduled jobs to display.")

    def get_job_info(self, job) -> Dict[str, Any]:
        """
        Get information about a job.

        :param job: The job.
        :return: A dictionary containing information about the job.
        """
        job_attributes = [
            "_scheduler",
            "_jobstore_alias",
            "id",
            "trigger",
            "executor",
            "func",
            "func_ref",
            "args",
            "kwargs",
            "name",
        ]
        return {attr: getattr(job, attr) for attr in job_attributes}

    def describe_all_job_functions(self) -> List[Dict[str, Any]]:
        """
        Describe all job functions along with their states.

        :return: A list of dictionaries containing details of each job function.
        """
        job_function_info_list = []
        for job in self.active_jobs:
            job_state = self.get_job_state(job).split("|")
            job_info = {
                "job_id": job.id,
                "job_name": job.name,
                "cron": f"{job.trigger}",
                "next_run_time": job_state[0],
                "status": job_state[1],
            }
            job_function_info_list.append(job_info)
        return job_function_info_list

    def get_job_state(self, job) -> str:
        """
        Get the state of a job.

        :param job: The job.
        :return: The state of the job.
        """
        if hasattr(job, "next_run_time") and job.next_run_time:
            if job.next_run_time > datetime.now():
                status = "pending"
                next_run_time = job.next_run_time
            else:
                status = "running"
                next_run_time = job.next_run_time
        else:
            status = "running"
            next_run_time = ""

        return f"{next_run_time}|{status}"
