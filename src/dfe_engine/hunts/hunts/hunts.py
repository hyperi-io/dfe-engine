import os
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any, List, Dict, Optional
from jinja2 import Environment
from hs_pylib.logger import logger
from .hunts_checkpoint_manager import HuntCheckpointManager
from ...clickhouse.clickhouse_manager import ClickHouseManager


class Hunt:
    CLICKHOUSE = "clickhouse"
    FILE = "file"

    base_logger_name_format = "{hunt_log_name}@{date}_PID=[{pid}]_THREADID={thread_id}"

    def __init__(
        self,
        cron: str,
        log_buffer: int,
        customer: str,
        rules: List[str],
        name: str,
        global_source_table_name: str,
        global_target_table_name: str,
        hunt_log_path: str,
        target_config_data: Dict,
        checkpoint_timestamp_field: str = "timestamp_load",
        customer_filters: Dict[str, Dict[str, str]] = None,
        checkpoint_destination: str = "clickhouse",
        hunt_checkpoint_path: Optional[str] = None,
        thread_id: str = None,
    ):
        """
        Initialize a new Hunt instance.

        Parameters:
        - cron (str): Cron expression for scheduling the hunt
        - log_buffer (int): Buffer time for logging
        - customer (str): customer ID
        - rules (List[str]): List of rules as strings
        - name (str): Name of the hunt
        - global_source_table_name (str): Default source table name
        - global_target_table_name (str): Default target table name
        - checkpoint_timestamp_field (str): The timestamp field name to be appended to rules (config based.)
        - hunt_log_path (str): Path to the hunt log
        - target_config_data (Dict): Target configuration data
        - customer_filters (Dict[str, Dict[str, str]], optional): Filters for a customer
        - checkpoint_destination (str, optional): Destination for checkpoints
        - hunt_checkpoint_path (str, optional): Path for checkpoint files
        - initial_checkpoint_lookback_minutes (int): Default look back value for when its the first execution of a hunt.
        """

        self.target_config_data = target_config_data
        self.name = name
        self.cron = cron
        self.log_buffer = log_buffer
        self.customer = customer
        self.rules = rules
        self.global_source_table_name = global_source_table_name
        self.global_target_table_name = global_target_table_name
        self.checkpoint_timestamp_field = checkpoint_timestamp_field
        self.checkpoint_destination = checkpoint_destination
        self.customer_filters = customer_filters or {}
        self.queries_by_customer = {}
        self.hunt_log_name = self._sanitize_hunt_name(self.name)
        self.hunt_log_path = hunt_log_path
        if not os.path.exists(hunt_log_path):
            os.makedirs(hunt_log_path, exist_ok=True)
        self.unique_id = str(uuid.uuid4())
        self.execution_time = datetime.now(timezone.utc)
        self.execution_time_str = self.execution_time.strftime("%Y-%m-%d_%H-%M-%S")
        self.successful_log_file_name = f"{self.hunt_log_name}_successful_queries_pid_{os.getpid()}_{self.execution_time_str}.log"
        self.unsuccessful_log_file_name = f"{self.hunt_log_name}_unsuccessful_queries_pid_{os.getpid()}_{self.execution_time_str}.log"
        self.pid = os.getpid()
        self.thread_id = thread_id
        self.hunt_checkpoint_path = hunt_checkpoint_path
        if hunt_checkpoint_path and not os.path.exists(hunt_checkpoint_path):
            os.makedirs(hunt_checkpoint_path, exist_ok=True)
        self.checkpoint_manager = HuntCheckpointManager()
        self.description = f"Hunt '{self.name}', ID: [{self.unique_id}], Scheduled: [{self.cron}], Number Rules: [{len(self.rules)}]"

    def __str__(self) -> str:
        """
        Returns a string representation of the Hunt instance,
        providing an overview of the initialized fields.
        """
        return (
            f"Hunt Details:\n"
            f"Name: {self.name}\n"
            f"ID: {self.unique_id}\n"
            f"Cron: {self.cron}\n"
            f"Log Buffer: {self.log_buffer} sec\n"
            f"Initial Look Back: {self.initial_checkpoint_lookback_minutes} minutes\n"
            f"Customer: {self.customer}\n"
            f"Rules: {self.rules}\n"
            f"Global Source Table Name: {self.global_source_table_name}\n"
            f"Global Target Table Name: {self.global_target_table_name}\n"
            f"Checkpoint Timestamp Field: {self.checkpoint_timestamp_field}\n"
            f"Customer Filters: {self.customer_filters}\n"
            f"Log Path: {self.hunt_log_path}\n"
            f"Thread ID: {self.thread_id}\n"
            f"Execution Time: {self.execution_time_str}\n"
            f"Checkpoint Destination: {self.checkpoint_destination}\n"
            f"Checkpoint Path: {self.hunt_checkpoint_path}"
        )

    def _sanitize_hunt_name(self, name: str) -> str:
        """
        Sanitize the hunt name by replacing spaces with underscores.

        Parameters:
        - name (str): Original hunt name

        Returns:
        - str: Sanitized hunt name
        """
        return name.replace(" ", "_")

    def convert_yaml_to_sql(
        self, env: Environment, org_id: str, customer_filters: Dict[str, Dict[str, str]]
    ) -> List[str]:
        """
        Convert YAML rules to SQL queries for a specific customer.

        Parameters:
        - env (Environment): Jinja2 environment for template rendering
        - org_id (str): Organization ID
        - customer_filters (Dict[str, Dict[str, str]]): Filters for the customer

        Returns:
        - List[str]: List of generated SQL queries
        """

        self.queries_by_customer[org_id] = []
        total_number_of_rules = len(self.rules)

        logger.debug(
            f"\n\n Building [{total_number_of_rules}] rules for [{org_id}] "
            f"to be executed at [{self.execution_time_str}] --- \n\n."
        )

        for rule_info in self.rules:
            rule_name = rule_info["rule_name"]
            target_table_name = rule_info.get("target_table_name", self.global_target_table_name)
            source_table_name = rule_info.get("source_table_name", self.global_source_table_name)
            self.initial_checkpoint_lookback_minutes = rule_info.get(
                "initial_checkpoint_lookback_minutes", self.convert_cron_to_minutes()
            )

            if not target_table_name or not source_table_name:
                logger.error(f"Missing table names for rule [{rule_name}]. Skipping this rule.")
                continue

            template = env.get_template(f"{rule_name}.jinja2")
            generated_sql = template.render(
                org_id=org_id,
                target_table_name=target_table_name,
                source_table_name=source_table_name,
                timestamp_condition="{timestamp_condition}",
                customer_filters="{customer_filters}",
            )

            org_filters = customer_filters.get(org_id, {})
            rules_filters = org_filters.get("rules", [])

            for filter_rule in rules_filters:
                if filter_rule.get("name") == rule_name:
                    filter_clause = filter_rule.get("filter_clause", "")
                    if filter_clause:
                        generated_sql = generated_sql.replace(
                            "{customer_filters}", f"({filter_clause})"
                        ).replace("{ customer_filters }", f"({filter_clause})")

            if "{customer_filters}" in generated_sql or "{ customer_filters }" in generated_sql:
                generated_sql = generated_sql.replace(
                    "{ customer_filters }", "{customer_filters}"
                ).replace("{customer_filters}", "True")

            self.queries_by_customer[org_id].append(generated_sql)

        return self.queries_by_customer[org_id]

    def build_sql_queries_for_customers(self, env: Environment) -> None:
        """
        Build SQL queries for each customer based on the provided environment.

        Parameters:
        - env (Environment): Jinja2 environment for template rendering
        """

        try:
            self.convert_yaml_to_sql(
                env, org_id=self.customer, customer_filters=self.customer_filters
            )
        except Exception as e:
            logger.warning(
                f"An error occurred while converting YAML to SQL for customer "
                f"'{self.customer}': {e}",
                exc_info=True,
            )
            raise e

    def convert_cron_to_minutes(self) -> int:
        """
        Converts a cron expression to minutes.

        :param cron_expression: The cron expression to convert
        :return: The frequency of the cron expression in minutes
        """
        index = 0
        minutes_per_index = [1, 1 * 60, 1 * 60 * 24]
        cron_list = self.cron.split()

        if self.cron == "* * * * *":
            return 1

        if "," in self.cron:
            return 1

        for field in cron_list:
            if index >= len(minutes_per_index):
                return -1
            elif "/" in field:
                schedule_val = int(field.split("/")[1])
                return schedule_val * minutes_per_index[index]
            else:
                index += 1
        return 0

    def execute_hunt(self, customer: str, scheduled_start_time: datetime) -> Dict[str, Any]:
        """
        Execute the hunt with the built SQL queries, considering the last successful run.
        Returns execution metrics including total execution time, number of successful queries,
        number of failed queries, etc.
        """
        execution_time = datetime.now(timezone.utc)
        execution_context = self._prepare_execution_context(scheduled_start_time, execution_time)

        logger.info(
            f"Running hunt '{self.name}' on pid [{self.pid}] with cron expression: "
            f"{self.cron} with customer [{customer}] @ Scheduled Start Time @ {execution_context['scheduled_start_time_str']} with buffer of {self.log_buffer} seconds "
            f"with initial_checkpoint_lookback_minutes {self.initial_checkpoint_lookback_minutes} minutes "
        )

        file_path = self._get_checkpoint_file_path(customer)

        try:
            successful_queries, failed_queries = self._execute_queries(execution_context, file_path)
        except Exception as e:
            base_error_message = (
                f"Hunt {self.name} failed during hunt execution. See specific log for {self.name} - "
                f"{self.pid} for more details: {str(e)}"
            )
            logger.error(base_error_message, exc_info=True)
            raise e

        total_execution_time = (datetime.now(timezone.utc) - execution_time).total_seconds()
        return {
            "total_execution_time": total_execution_time,
            "successful_queries": successful_queries,
            "failed_queries": failed_queries,
            "hunt_name": self.name,
        }

    def _prepare_execution_context(
        self, scheduled_start_time: datetime, execution_time: datetime
    ) -> Dict[str, Any]:
        """Prepare execution context with timestamps and formatting."""
        scheduled_start_time_w_buffer = scheduled_start_time - timedelta(seconds=self.log_buffer)
        return {
            "execution_time": execution_time,
            "execution_time_str": execution_time.strftime("%Y-%m-%d %H:%M:%S"),
            "scheduled_start_time": scheduled_start_time,
            "scheduled_start_time_str": scheduled_start_time.strftime("%Y-%m-%d %H:%M:%S"),
            "scheduled_start_time_w_buffer": scheduled_start_time_w_buffer,
            "scheduled_start_time_w_buffer_str": scheduled_start_time_w_buffer.strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
        }

    def _get_checkpoint_file_path(self, customer: str) -> Optional[str]:
        """Get checkpoint file path if using file-based checkpointing."""
        if self.checkpoint_destination == self.FILE:
            self.checkpoint_manager.ensure_checkpoint_file_path_exists(self.hunt_checkpoint_path)
            return os.path.join(
                self.hunt_checkpoint_path,
                f"hunt_checkpoints_{customer}_{self.execution_time_str}.json".replace("_", "-"),
            )
        return None

    def _execute_queries(
        self, execution_context: Dict[str, Any], file_path: Optional[str]
    ) -> tuple:
        """Execute all queries and return success/failure counts."""
        successful_queries = 0
        failed_queries = 0

        with ClickHouseManager.get_instance(
            self.target_config_data
        ).get_clickhouse_client() as ch_client:
            successful_checkpoints = []
            for customer, list_query in self.queries_by_customer.items():
                total_number_of_queries = len(list_query)
                rule_counter = 0
                logger.info(
                    f"Running hunt '{self.name}' on pid [{self.pid}] with cron expression: "
                    f"{self.cron} with [{len(list_query)}] queries with Buffer "
                    f"set to: {self.log_buffer}. Executing @{execution_context['execution_time_str']} Customer @ {customer} Scheduled Start Time @ {execution_context['scheduled_start_time_str']} with buffer of {self.log_buffer} seconds"
                )

                for index, query in enumerate(list_query):
                    result = self._execute_single_query(
                        ch_client,
                        query,
                        index,
                        customer,
                        execution_context,
                        file_path,
                        total_number_of_queries,
                    )

                    if result["success"]:
                        rule_counter += 1
                        successful_queries += 1
                        successful_checkpoints.append(result["checkpoint"])
                        logger.debug(
                            f"Hunt [{self.name}] executed ({rule_counter}/{total_number_of_queries}) "
                            f"for org [{customer}] executed @{execution_context['execution_time_str']} previous checkpoint {result['last_success_time']} new checkpoint @{execution_context['scheduled_start_time_w_buffer_str']}"
                        )
                    else:
                        failed_queries += 1

            self._save_checkpoints(ch_client, successful_checkpoints, file_path)

        return successful_queries, failed_queries

    def _execute_single_query(
        self,
        ch_client,
        query: str,
        index: int,
        customer: str,
        execution_context: Dict[str, Any],
        file_path: Optional[str],
        total_number_of_queries: int,
    ) -> Dict[str, Any]:
        """Execute a single query and return result with checkpoint data."""
        rule = self.rules[index]
        generated_query_id = str(uuid.uuid4())

        last_success_time = self.checkpoint_manager.get_last_successful_run(
            checkpoint_destination=self.checkpoint_destination,
            ch_client=ch_client,
            customer=customer,
            hunt_name=self.name,
            rule_name=rule["rule_name"],
            file_path=file_path,
        )

        last_success_time, last_success_time_str = self._resolve_last_success_time(
            last_success_time, execution_context
        )

        timestamp_condition = f"({self.checkpoint_timestamp_field} >= '{last_success_time_str}' AND {self.checkpoint_timestamp_field} < '{execution_context['scheduled_start_time_w_buffer_str']}')"

        query = query.replace("{timestamp_condition}", timestamp_condition).replace(
            "{ timestamp_condition }", timestamp_condition
        )

        try:
            logger.info(
                f"-------- DFE Hunt {self.name} Executing --------- \n"
                f"Executing [{total_number_of_queries}] rules for [{self.name}] "
                f"with CRON [{self.cron}] executing at [{execution_context['execution_time_str']}] For "
                f"Timestamp Condition [{timestamp_condition}]."
            )

            query_start_time = datetime.now(timezone.utc)
            query_result = ch_client.execute(query, query_id=generated_query_id)
            query_end_time = datetime.now(timezone.utc)
            query_execution_time_ms = (query_end_time - query_start_time).total_seconds() * 1000

            logger.debug(
                f"Rule [{rule['rule_name']}] for org [{customer}] "
                f"successfully executed in [{query_execution_time_ms}] ms \n"
                f"Query Results: [{query_result}]"
            )

            checkpoint = {
                "checkpoint_destination": self.checkpoint_destination,
                "customer_name": customer,
                "rule_name": rule["rule_name"],
                "hunt_name": self.name,
                "query_id": generated_query_id,
                "thread_id": self.thread_id,
                "log_buffer": self.log_buffer,
                "query_schedule_time": execution_context["scheduled_start_time"].strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
                "execution_time": query_start_time.strftime("%Y-%m-%d %H:%M:%S"),
                "end_time": query_end_time.strftime("%Y-%m-%d %H:%M:%S"),
                "previous_successful_checkpoint": last_success_time.strftime("%Y-%m-%d %H:%M:%S"),
                "query_checkpoint_time": execution_context[
                    "scheduled_start_time_w_buffer"
                ].strftime("%Y-%m-%d %H:%M:%S"),
                "execution_time_ms": query_execution_time_ms,
                "file_path": file_path,
            }

            return {
                "success": True,
                "checkpoint": checkpoint,
                "last_success_time": last_success_time,
            }

        except Exception as e:
            error_message = str(e).split("Stack trace")[0]
            logger.error(
                f"Hunt {self.name} failed\n error: {str(e)}\n SQL ---> [{query}] ERROR MESSAGE [{error_message}] \n"
            )
            return {"success": False}

    def _resolve_last_success_time(
        self, last_success_time: Optional[datetime], execution_context: Dict[str, Any]
    ) -> tuple:
        """Resolve the last success time, using lookback if none exists."""
        if last_success_time is None:
            minute_schedule = self.convert_cron_to_minutes()
            if minute_schedule == -1:
                logger.error(
                    f"Cannot convert cron '{self.cron}' to minutes - likely a variable months value"
                )
                raise Exception(
                    f"Cannot convert cron '{self.cron}' to minutes - likely a variable months value"
                )
            look_back_in_minutes = timedelta(minutes=self.initial_checkpoint_lookback_minutes)
            last_success_time = (
                execution_context["scheduled_start_time_w_buffer"] - look_back_in_minutes
            )
            last_success_time_str = last_success_time.strftime("%Y-%m-%d %H:%M:%S")
            logger.warning(
                f"No previous successful run for {self.name}. Initial run will be run using the cron job candence generated last success time '{last_success_time_str}'."
            )
        else:
            logger.debug(
                f"Last successful run for {self.name}: {last_success_time} to query against checkpoint field {self.checkpoint_timestamp_field}"
            )
            last_success_time_str = last_success_time.strftime("%Y-%m-%d %H:%M:%S")

        return last_success_time, last_success_time_str

    def _save_checkpoints(
        self, ch_client, successful_checkpoints: List[Dict], file_path: Optional[str]
    ) -> None:
        """Save checkpoints to the appropriate destination."""
        if self.checkpoint_destination == Hunt.CLICKHOUSE:
            self.checkpoint_manager.create_batch_checkpoint_clickhouse(
                ch_client, successful_checkpoints
            )
        else:
            self.checkpoint_manager.create_batch_checkpoint_file(successful_checkpoints, file_path)
