import json
import os
import re
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from scalo.logger import logger

from dfe_engine.schema.applier import SchemaApplier
from dfe_engine.schema.ddl_writer import DDLFileWriter
from dfe_engine.schema.engine_resolver import EngineResolver
from dfe_engine.settings import default_data_database

_SAFE_IDENTIFIER = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def _validate_identifier(name: str, label: str = "identifier") -> str:
    """Validate a ClickHouse identifier against an allowlist pattern.

    ClickHouse does not support parameterised identifiers (database/table names),
    so we validate them before interpolation to prevent SQL injection.
    """
    if not _SAFE_IDENTIFIER.match(name):
        raise ValueError(f"Invalid {label}: {name!r}")
    return name


def _utc(value: str) -> datetime:
    """Parse a checkpoint timestamp, which callers write in UTC.

    A naive datetime is inserted as the host's local time, which moves every
    checkpoint by the host's UTC offset on any host not set to UTC.
    """
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)


class Status(Enum):
    SUCCESS = "success"
    FAIL = "fail"


class HuntCheckpointManager:
    _DETECTION_CHECKPOINT_TABLE_NAME: str = "detection_checkpoint"
    _DEFAULT_DATABASE_NAME: str = default_data_database()

    CLICKHOUSE = "clickhouse"
    FILE = "file"

    def __init__(
        self,
        database_name: str | None = None,
        table_name: str | None = None,
    ) -> None:
        """
        Initialize the HuntCheckpointManager.

        Args:
            table_name (Optional[str]): Custom table name.
            database_name (Optional[str]): Custom database name.
        """
        self.database_name: str = _validate_identifier(
            database_name or self._DEFAULT_DATABASE_NAME, "database"
        )
        self.table_name: str = _validate_identifier(
            table_name or self._DETECTION_CHECKPOINT_TABLE_NAME, "table"
        )

    def database_exists(self, ch_client, database_name: str) -> bool:
        """
        Check if the database exists in ClickHouse.

        Args:
            ch_client: ClickHouse client instance.
            database_name (str): Name of the database to check.

        Returns:
            bool: True if the database exists, False otherwise.
        """

        try:
            result = ch_client.execute(
                "SELECT 1 FROM system.databases WHERE name = %(db_name)s",
                parameters={"db_name": database_name},
            )
            return bool(result)
        except Exception as e:
            logger.error(
                "Hunt Checkpoint: error checking if database exists",
                database_name=database_name,
                error=str(e),
                exc_info=True,
            )
            return False

    def drop_database_and_table(self, ch_client, database_name, table_name) -> None:
        """
        Drop the database and table using the unique suffix.

        Args:
            ch_client: ClickHouse client instance.
            database_name (str): Name of the database to drop.
            table_name (str): Name of the table to drop.
        """
        db = _validate_identifier(database_name, "database")
        tbl = _validate_identifier(table_name, "table")
        try:
            if self.table_exists(ch_client, db, tbl):
                ch_client.execute(f"DROP TABLE IF EXISTS {db}.{tbl}")
                logger.info(f"Table [{db}.{tbl}] dropped successfully.")

            if self.database_exists(ch_client, db):
                ch_client.execute(f"DROP DATABASE IF EXISTS {db}")
                logger.info(f"Database [{db}] dropped successfully.")
        except Exception as e:
            logger.error(f"Failed to drop database or table: {e}", exc_info=True)

    def table_exists(self, ch_client, database_name: str, table_name: str) -> bool:
        """
        Check if the table exists in ClickHouse.

        Args:
            ch_client: ClickHouse client instance.
            database_name (str): Name of the database.
            table_name (str): Name of the table.

        Returns:
            bool: True if the table exists, False otherwise.
        """

        try:
            result = ch_client.execute(
                "SELECT 1 FROM system.tables WHERE database = %(db_name)s AND name = %(tbl_name)s",
                parameters={"db_name": database_name, "tbl_name": table_name},
            )
            return bool(result)
        except Exception as e:
            logger.error(f"Error checking table existence: {e}", exc_info=True)
            return False

    def ensure_checkpoint_file_path_exists(self, hunt_checkpoint_path: str) -> None:
        directory = os.path.dirname(hunt_checkpoint_path)
        os.makedirs(directory, exist_ok=True)

    def ensure_table_exists(
        self,
        ch_client,
        create_missing_tables: bool = True,
        create_missing_database: bool = True,
    ) -> bool:
        """Create or reconcile the checkpoint table in ClickHouse.

        The columns come from the dfe-schemas ``hunts/detection_checkpoint``
        definition and the engine clause from sensing the target server, so a
        column added to the schema is added to an existing table and a cluster
        gets the table on every replica.

        Args:
            ch_client: ClickHouse client instance.
            create_missing_tables: Whether to create the table if it is absent.
            create_missing_database: Whether to create the database if it is absent.

        Returns:
            True if the table exists or was created, False otherwise.
        """
        if not create_missing_database and not self.database_exists(ch_client, self.database_name):
            logger.info("Missing Database and not creating it")
            return False
        if not create_missing_tables and not self.table_exists(
            ch_client, self.database_name, self.table_name
        ):
            logger.info(f"Table [{self.database_name}.{self.table_name}] is absent.")
            return False

        try:
            spec = DDLFileWriter(
                resolver=EngineResolver(client=ch_client), database=self.database_name
            ).detection_checkpoint_table_spec()
            applier = SchemaApplier(ch_client, EngineResolver(client=ch_client))
            if create_missing_database:
                applier.ensure_database(self.database_name)
            change = applier.ensure_table(
                self.database_name, self.table_name, spec.columns, spec.config
            )
            logger.debug(f"HuntCheckpointManager: {change.describe()}")
            return True
        except Exception as e:
            logger.error(f"Error ensuring checkpoint table: {e}", exc_info=True)
            return False

    def get_last_successful_run(
        self,
        checkpoint_destination: str = "clickhouse",
        ch_client=None,
        customer: str = "",
        hunt_name: str = "",
        rule_name: str = "",
        file_path: str = "",
    ) -> datetime | None:
        """
        A method to fetch the last successful run's timestamp based on the destination type.

        Args:
            checkpoint_destination: FILE or CLICKHOUSE.
            ch_client: ClickHouse client (if CLICKHOUSE).
            hunt_name: Hunt name.
            rule_name: Rule name.
            file_path: File path (if FILE).

        Returns:
            Timestamp of the last successful run, if any.
        """
        if checkpoint_destination == self.FILE:
            logger.info("Checkpoints will be read from file", path=file_path)
            return self.get_last_successful_run_file(
                customer=customer,
                hunt_name=hunt_name,
                rule_name=rule_name,
                file_path=file_path,
            )
        else:  # CLICKHOUSE
            self.ensure_table_exists(ch_client)
            tbl = f"{self.database_name}.{self.table_name}"
            logger.debug("Checkpoints will be read from ClickHouse", table=tbl)
            return self.get_last_successful_run_clickhouse(
                customer=customer,
                ch_client=ch_client,
                hunt_name=hunt_name,
                rule_name=rule_name,
            )

    def get_last_successful_run_clickhouse(
        self,
        ch_client,
        hunt_name: str,
        rule_name: str,
        customer: str,
    ) -> datetime | None:
        """
        Fetch the last successful run's timestamp from ClickHouse.

        Args:
            ch_client: ClickHouse client instance.
            hunt_name (str): Hunt name.
            rule_name (str): Rule name.
            customer (str): Customer name.
            logger (logging.Logger): Logger instance.

        Returns:
            Optional[datetime]: The timestamp of the last successful run, if any.
        """

        query = (
            f"SELECT max(query_checkpoint_time) AS last_success_time "
            f"FROM {self.database_name}.{self.table_name} "
            "WHERE rule_name = %(rule_name)s "
            "AND hunt_name = %(hunt_name)s "
            "AND _org_id = %(customer)s "
            "ORDER BY last_success_time DESC LIMIT 1"
        )
        params = {
            "rule_name": rule_name,
            "hunt_name": hunt_name,
            "customer": customer,
        }

        logger.debug("Checkpoint query", query=query, params=params)

        try:
            result = ch_client.execute(query, parameters=params)
            if result and result != [(datetime(1970, 1, 1, 0, 0),)]:
                last_success_time = result[0][0]
                logger.debug(f"Last Successful Checkpoint Time: {last_success_time}")
                return last_success_time
            else:
                logger.info("No results found.")
                return None
        except Exception as e:
            logger.error(f"Failed to retrieve last successful run: {e}", exc_info=True)
            return None

    def get_last_successful_run_file(
        self,
        hunt_name: str,
        rule_name: str,
        customer: str,
        file_path: str,
    ) -> datetime | None:
        """
        Fetch the last successful run's timestamp from file.

        Args:
            hunt_name (str): Hunt name.
            rule_name (str): Rule name.
            customer (str): Org id (tenant identifier).
            file_path (str): Path to the checkpoint file.
            logger (logging.Logger): Logger instance.

        Returns:
            Optional[datetime]: The timestamp of the last successful run, if any.
        """

        try:
            with open(file_path) as file:
                checkpoints = json.load(file)

            relevant_checkpoints = [
                cp
                for cp in checkpoints
                if cp["_org_id"] == customer
                and cp["hunt_name"] == hunt_name
                and cp["rule_name"] == rule_name
            ]
            logger.debug(f"relevant_checkpoints value is {relevant_checkpoints}")
            if relevant_checkpoints:
                last_success_time = max(
                    datetime.strptime(cp["query_checkpoint_time"], "%Y-%m-%d %H:%M:%S")
                    for cp in relevant_checkpoints
                )
                logger.info(f"Last Success Time Executed: {last_success_time}")
                return last_success_time
            else:
                logger.info("No results found.")
                return None
        except FileNotFoundError:
            logger.info("No checkpoint file found.")
            return None
        except Exception as e:
            logger.error(f"Failed to retrieve last successful run: {e}", exc_info=True)
            return None

    def checkpoint_rule(
        self,
        checkpoint_destination: str,
        ch_client=None,
        customer: str = "",
        rule: str = "",
        hunt_name: str = "",
        thread_id: str = "",
        query_id: str = "",
        log_buffer: int = 0,
        end_time_str: str = "",
        execution_time_ms: int = 0,
        execution_time_str: str = "",
        query_schedule_time_str: str = "",
        previous_successful_checkpoint_str: str = "",
        query_checkpoint_time_str: str = "",
        file_path: str = "",
    ) -> None:
        """
        A method to create or update a checkpoint based on the destination type.

        Args:
            checkpoint_destination: FILE or CLICKHOUSE.
            ch_client: ClickHouse client (if CLICKHOUSE).
            customer: Customer name.
            rule (str, optional): Rule name.
            hunt_name (str, optional): Hunt name.
            query_id (str, optional): Query ID.
            log_buffer (int, optional): Query ID.
            query_schedule_time (str, optional): Query ID.
            execution_time (datetime): Execution time.
            end_time (datetime): End time.
            previous_successful_checkpoint_str (str) : Last successfull query previously detected.
            query_checkpoint_time (str): Query Checkpoint time.
            execution_time_ms (int, optional): Execution time in milliseconds.
            logger (logging.Logger, optional): Logger.
            file_path (str, optional): File path, needed if checkpoint destination is FILE.
        """
        if checkpoint_destination == self.FILE:
            self.ensure_checkpoint_file_path_exists(file_path)
            self.create_checkpoint_file(
                customer=customer,
                rule=rule,
                hunt_name=hunt_name,
                query_id=query_id,
                log_buffer=log_buffer,
                end_time_str=end_time_str,
                execution_time_str=execution_time_str,
                query_schedule_time_str=query_schedule_time_str,
                previous_successful_checkpoint_str=previous_successful_checkpoint_str,
                query_checkpoint_time_str=query_checkpoint_time_str,
                thread_id=thread_id,
                execution_time_ms=execution_time_ms,
                file_path=file_path,
            )
        else:  # CLICKHOUSE
            self.ensure_table_exists(ch_client)
            self.create_checkpoint_clickhouse(
                ch_client=ch_client,
                customer=customer,
                rule=rule,
                hunt_name=hunt_name,
                query_id=query_id,
                log_buffer=log_buffer,
                end_time_str=end_time_str,
                execution_time_str=execution_time_str,
                query_schedule_time_str=query_schedule_time_str,
                previous_successful_checkpoint_str=previous_successful_checkpoint_str,
                query_checkpoint_time_str=query_checkpoint_time_str,
                thread_id=thread_id,
                execution_time_ms=execution_time_ms,
            )

    def create_checkpoint_clickhouse(
        self,
        ch_client,
        customer: str,
        rule: str,
        hunt_name: str,
        query_id: str,
        log_buffer: int,
        query_schedule_time_str: str,
        execution_time_str: str,
        end_time_str: str,
        previous_successful_checkpoint_str: str,
        query_checkpoint_time_str: str,
        execution_time_ms: int,
        thread_id: str = "",
    ) -> None:
        """
        Insert the checkpoint data into the ClickHouse table.

        Args:
            ch_client,
            customer (str): Customer name.
            rule (str): Rule name.
            hunt_name (str): Hunt name.
            query_id (str): Query ID.
            execution_time (datetime): Execution time.
            end_time (datetime): End time.
            previous_successful_checkpoint (str) : Last successfull query previously detected.
            query_checkpoint_time_str (datetime): Query Checkpoint time.
            execution_time_ms (int): Execution time in milliseconds.
            logger (logging.Logger): Logger instance.
            thread_id (str): Thread ID.
        """

        if not (customer or "").strip():
            logger.warning(
                "Checkpoint: refusing ClickHouse checkpoint with empty _org_id "
                f"(hunt={hunt_name}, rule={rule})"
            )
            return

        try:
            insert_sql = (
                f"INSERT INTO {self.database_name}.{self.table_name} "
                "(_org_id, rule_name, thread_id, log_buffer, "
                "query_schedule_time, execution_time, end_time, "
                "previous_successful_checkpoint, query_checkpoint_time, "
                "execution_time_ms, hunt_name, query_id) VALUES"
            )
            # clickhouse-connect's insert() needs real datetime objects for the
            # DateTime columns, not strings (the batch path already does this).
            data = [
                (
                    customer,
                    rule,
                    thread_id,
                    int(log_buffer),
                    _utc(query_schedule_time_str),
                    _utc(execution_time_str),
                    _utc(end_time_str),
                    _utc(previous_successful_checkpoint_str),
                    _utc(query_checkpoint_time_str),
                    execution_time_ms,
                    hunt_name,
                    query_id,
                )
            ]
            logger.debug("Checkpoint insert", table=f"{self.database_name}.{self.table_name}")
            ch_client.execute(insert_sql, data)
        except Exception as e:
            logger.error(f"Failed to create checkpoint: {e}", exc_info=True)

    def create_checkpoint_file(
        self,
        customer: str,
        rule: str,
        hunt_name: str,
        query_id: str,
        thread_id: str,
        log_buffer: int,
        previous_successful_checkpoint_str: str,
        query_schedule_time_str: str,
        execution_time_str: str,
        end_time_str: str,
        query_checkpoint_time_str: str,
        execution_time_ms: int,
        file_path: str = "",
    ) -> None:
        """
        Create the checkpoint file.

        Args:
            customer (str): Customer name.
            rule (str): Rule name.
            hunt_name (str): Hunt name.
            query_id (str): Query ID.
            execution_time (datetime): Execution time.
            end_time (datetime): End time.
            query_checkpoint_time (datetime): Query Checkpoint time.
            execution_time_ms (int): Execution time in milliseconds.
            logger (logging.Logger): Logger instance.
            thread_id (str): Thread ID.
            file_path (str): Path to the checkpoint file.
        """

        if not (customer or "").strip():
            logger.warning(
                "Checkpoint: refusing file checkpoint with empty _org_id "
                f"(hunt={hunt_name}, rule={rule})"
            )
            return

        try:
            if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
                with open(file_path) as file:
                    checkpoints = json.load(file)
            else:
                checkpoints = []

            checkpoint = {
                "_org_id": customer,
                "rule_name": rule,
                "hunt_name": hunt_name,
                "log_buffer": log_buffer,
                "previous_successful_checkpoint": previous_successful_checkpoint_str,
                "query_checkpoint_time": query_checkpoint_time_str,
                "query_schedule_time": query_schedule_time_str,
                "execution_time": execution_time_str,
                "execution_time_ms": execution_time_ms,
                "end_time": end_time_str,
                "thread_id": thread_id,
                "query_id": query_id,
            }

            checkpoints.append(checkpoint)

            with open(file_path, "w") as file:
                json.dump(checkpoints, file, indent=4)

            logger.info(f"Checkpoint created successfully in file [{file_path}]")
        except Exception as e:
            logger.error(f"Failed to create checkpoint: {e}", exc_info=True)

    def create_batch_checkpoint_clickhouse(self, ch_client, checkpoints: list[dict[str, Any]]):
        """
        Insert the checkpoint data into the ClickHouse table.

        Args:
            ch_client: ClickHouse client.
            checkpoints (List[Dict[str, Any]]): List of checkpoint data.
            logger (logging.Logger): Logger instance.
        """

        logger.debug(f"Starting batch checkpointing into [{self.database_name}.{self.table_name}].")
        valid_checkpoints = []
        for cp in checkpoints:
            if not str(cp.get("_org_id") or "").strip():
                logger.warning(
                    "Checkpoint: skipping batch checkpoint with empty _org_id "
                    f"(hunt={cp.get('hunt_name', 'na')}, rule={cp.get('rule_name', 'na')})"
                )
                continue
            valid_checkpoints.append(cp)
        if not valid_checkpoints:
            return
        try:
            data = [
                (
                    str(checkpoint.get("_org_id", "na")),
                    str(checkpoint.get("rule_name", "na")),
                    str(checkpoint.get("thread_id", "na")),
                    int(checkpoint.get("log_buffer", 0)),
                    _utc(checkpoint["query_schedule_time"]),
                    _utc(checkpoint["execution_time"]),
                    _utc(checkpoint["end_time"]),
                    _utc(checkpoint["previous_successful_checkpoint"]),
                    _utc(checkpoint["query_checkpoint_time"]),
                    int(checkpoint.get("execution_time_ms", 0)),
                    str(checkpoint.get("hunt_name", "na")),
                    str(checkpoint.get("query_id", "na")),
                    str(checkpoint.get("explain_plan") or ""),
                    int(checkpoint.get("explain_duration_ms") or 0),
                    str(checkpoint.get("scheduling_mode", "adaptive")),
                    int(checkpoint.get("read_rows") or 0),
                    int(checkpoint.get("read_bytes") or 0),
                    int(checkpoint.get("memory_usage") or 0),
                    int(checkpoint.get("result_rows") or 0),
                    str(checkpoint.get("query_fingerprint") or ""),
                )
                for checkpoint in valid_checkpoints
            ]

            ch_client.execute(
                f"""
                INSERT INTO {self.database_name}.{self.table_name}
                (
                    _org_id,
                    rule_name,
                    thread_id,
                    log_buffer,
                    query_schedule_time,
                    execution_time,
                    end_time,
                    previous_successful_checkpoint,
                    query_checkpoint_time,
                    execution_time_ms,
                    hunt_name,
                    query_id,
                    explain_plan,
                    explain_duration_ms,
                    scheduling_mode,
                    read_rows,
                    read_bytes,
                    memory_usage,
                    result_rows,
                    query_fingerprint
                ) VALUES
                """,
                data,
            )
            tbl = f"{self.database_name}.{self.table_name}"
            logger.debug("Batch checkpoint created", table=tbl)
        except Exception as e:
            logger.error(f"Failed to create batch checkpoints: {e}", exc_info=True)

    def create_batch_checkpoint_file(
        self,
        checkpoints: list[dict[str, Any]],
        file_path: str,
    ) -> None:
        """
        Create or append to the checkpoint file.

        Args:
            checkpoints (List[Dict[str, Any]]): List of checkpoint data.
            file_path (str): Path to the checkpoint file.
        """
        directory = os.path.dirname(file_path)
        if not os.path.exists(directory):
            os.makedirs(directory, exist_ok=True)
        try:
            if not os.path.exists(file_path):
                with open(file_path, "w") as file:
                    json.dump(checkpoints, file, indent=4)
            else:
                with open(file_path, "r+") as file:
                    existing_checkpoints = json.load(file)
                    existing_checkpoints.extend(checkpoints)
                    file.seek(0)
                    json.dump(existing_checkpoints, file, indent=4)

            logger.debug(f"Checkpoints successfully appended/created in file [{file_path}]")
        except Exception as e:
            logger.error(f"Failed to create or append checkpoints: {e}", exc_info=True)
