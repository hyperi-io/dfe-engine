import os
from typing import Any, Dict, List, Optional
from datetime import datetime
from enum import Enum
from hs_pylib.logger import logger

import json


class Status(Enum):
    SUCCESS = "success"
    FAIL = "fail"


class HuntCheckpointManager:
    _DETECTION_CHECKPOINT_TABLE_NAME: str = "detection_checkpoint"
    _AUDIT_DATABASE_NAME: str = "dfe_audit"

    CLICKHOUSE = "clickhouse"
    FILE = "file"

    def __init__(
        self,
        database_name: Optional[str] = None,
        table_name: Optional[str] = None,
    ) -> None:
        """
        Initialize the HuntCheckpointManager.

        Args:
            table_name (Optional[str]): Custom table name.
            database_name (Optional[str]): Custom database name.
        """
        self.database_name: str = f"{database_name or self._AUDIT_DATABASE_NAME}"
        self.table_name: str = f"{table_name or self._DETECTION_CHECKPOINT_TABLE_NAME}"

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
                f"SELECT 1 FROM system.databases WHERE name = '{database_name}'"
            )
            return bool(result)
        except Exception as e:
            logger.error(
                f"Hunt Checkpoint: An error occurred while checking if the database exists: {e}",
                exc_info=True,
            )
            return False

    def drop_database_and_table(self, ch_client, database_name, table_name) -> None:
        """
        Drop the database and table using the unique suffix.

        Args:
            ch_client: ClickHouse client instance.
            unique_number (int): The unique number used for the suffix.
        """
        try:
            if self.table_exists(ch_client, database_name, table_name):
                ch_client.execute(f"DROP TABLE IF EXISTS {database_name}.{table_name};")
                logger.info(f"Table [{database_name}.{table_name}] dropped successfully.")

            if self.database_exists(ch_client, database_name):
                ch_client.execute(f"DROP DATABASE IF EXISTS {database_name};")
                logger.info(f"Database [{database_name}] dropped successfully.")
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
                f"SELECT 1 FROM system.tables WHERE database = '{database_name}' AND name = '{table_name}'"
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
        no_cluster_declarations_needed: bool = True,
    ) -> bool:
        """
        Ensure that the required table exists in ClickHouse.

        Args:
            ch_client: ClickHouse client instance.
            create_missing_tables (bool): Whether to create the table if it doesn't exist.
            create_missing_database (bool): Whether to create the database if it doesn't exist.
            no_cluster_declarations_needed (bool): Whether cluster declarations are needed.

        Returns:
            bool: True if the table exists or is created successfully, False otherwise.
        """

        if create_missing_database and not self.database_exists(ch_client, self.database_name):
            try:
                ch_client.execute(f"CREATE DATABASE IF NOT EXISTS {self.database_name};")
            except Exception as e:
                logger.error(f"Error creating database: {e}")
                return False
        elif not create_missing_database:
            logger.info("Missing Database and not creating it")
            return False

        if create_missing_tables and not self.table_exists(
            ch_client, self.database_name, self.table_name
        ):
            try:
                engine = "MergeTree()" if no_cluster_declarations_needed else "SharedMergeTree()"
                create_table_sql = f"""
                CREATE TABLE IF NOT EXISTS {self.database_name}.{self.table_name} (
                    customer_name LowCardinality(String) CODEC(LZ4),
                    rule_name LowCardinality(String) CODEC(LZ4),
                    thread_id LowCardinality(String) CODEC(LZ4),
                    log_buffer UInt32 CODEC(Delta, ZSTD),
                    query_schedule_time DateTime CODEC(DoubleDelta, LZ4),
                    execution_time DateTime CODEC(DoubleDelta, LZ4),
                    end_time DateTime CODEC(DoubleDelta, LZ4),
                    previous_successful_checkpoint DateTime CODEC(DoubleDelta, LZ4),
                    query_checkpoint_time DateTime CODEC(DoubleDelta, LZ4),
                    execution_time_ms Int32 CODEC(Delta, ZSTD),
                    hunt_name LowCardinality(String) CODEC(LZ4),
                    query_id LowCardinality(String) CODEC(LZ4)
                ) ENGINE = {engine}
                PARTITION BY toYYYYMM(query_checkpoint_time)
                ORDER BY (customer_name, hunt_name, rule_name, query_checkpoint_time);
                """
                ch_client.execute(create_table_sql)
            except Exception as e:
                logger.error(f"Error creating table: {e}", exc_info=True)
                return False
        elif not create_missing_tables:
            logger.info(f"Table [{self.database_name}.{self.table_name}] already exists.")
            return False

        return True

    def get_last_successful_run(
        self,
        checkpoint_destination: str = "clickhouse",
        ch_client=None,
        customer: str = "",
        hunt_name: str = "",
        rule_name: str = "",
        file_path: str = "",
    ) -> Optional[datetime]:
        """
        A method to fetch the last successful run's timestamp based on the destination type.

        Args:
            checkpoint_destination (str): The destination type for the checkpoint (FILE or CLICKHOUSE).
            ch_client (optional): ClickHouse client, needed if checkpoint destination is CLICKHOUSE.
            hunt_name (str, optional): Hunt name.
            rule_name (str, optional): Rule name.
            file_path (str, optional): File path, needed if checkpoint destination is FILE.
            logger (logging.Logger, optional): Logger.

        Returns:
            Optional[datetime]: The timestamp of the last successful run, if any.
        """
        if checkpoint_destination == self.FILE:
            logger.info(f"Checkpoints will be read from file [{file_path}]")
            return self.get_last_successful_run_file(
                customer=customer,
                hunt_name=hunt_name,
                rule_name=rule_name,
                file_path=file_path,
            )
        else:  # CLICKHOUSE
            self.ensure_table_exists(ch_client)
            logger.debug(
                f"Checkpoints will be read from ClickHouse table [{self.database_name}.{self.table_name}]"
            )
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
    ) -> Optional[datetime]:
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

        query = f"""
            SELECT 
                max(query_checkpoint_time) AS last_success_time
            FROM {self.database_name}.{self.table_name}
            WHERE rule_name = '{rule_name}' AND  hunt_name = '{hunt_name}' AND customer_name = '{customer}'
            ORDER BY last_success_time DESC
            LIMIT 1;
        """

        logger.debug(f"CheckPoint Query: {query}")

        try:
            result = ch_client.execute(query)
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
    ) -> Optional[datetime]:
        """
        Fetch the last successful run's timestamp from file.

        Args:
            hunt_name (str): Hunt name.
            rule_name (str): Rule name.
            customer_name (str): Customer name.
            file_path (str): Path to the checkpoint file.
            logger (logging.Logger): Logger instance.

        Returns:
            Optional[datetime]: The timestamp of the last successful run, if any.
        """

        try:
            with open(file_path, "r") as file:
                checkpoints = json.load(file)

            relevant_checkpoints = [
                cp
                for cp in checkpoints
                if cp["customer_name"] == customer
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
        log_buffer: int = None,
        end_time_str: str = None,
        execution_time_ms: int = 0,
        execution_time_str: str = None,
        query_schedule_time_str: str = None,
        previous_successful_checkpoint_str: str = None,
        query_checkpoint_time_str: str = None,
        file_path: str = "",
    ) -> None:
        """
        A method to create or update a checkpoint based on the destination type.

        Args:
            checkpoint_destination (str): The destination type for the checkpoint (FILE or CLICKHOUSE).
            ch_client (optional): ClickHouse client, needed if checkpoint destination is CLICKHOUSE.
            customer (str, optional): Customer name.
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

        try:
            insert_sql = f"""
                INSERT INTO {self.database_name}.{self.table_name} (
                    customer_name, 
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
                    query_id
                    ) 
                VALUES (
                    '{customer}', 
                    '{rule}',
                    '{thread_id}',
                    '{log_buffer}', 
                    '{query_schedule_time_str}', 
                    '{execution_time_str}',
                    '{end_time_str}',
                    '{previous_successful_checkpoint_str}', 
                    '{query_checkpoint_time_str}', 
                    {execution_time_ms}, 
                    '{hunt_name}', 
                    '{query_id}'
                    )
            """
            logger.debug(insert_sql)
            ch_client.execute(insert_sql)
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

        try:
            if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
                with open(file_path, "r") as file:
                    checkpoints = json.load(file)
            else:
                checkpoints = []

            checkpoint = {
                "customer_name": customer,
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

    def create_batch_checkpoint_clickhouse(self, ch_client, checkpoints: List[Dict[str, Any]]):
        """
        Insert the checkpoint data into the ClickHouse table.

        Args:
            ch_client: ClickHouse client.
            checkpoints (List[Dict[str, Any]]): List of checkpoint data.
            logger (logging.Logger): Logger instance.
        """

        logger.debug(f"Starting batch checkpointing into [{self.database_name}.{self.table_name}].")
        try:
            data = [
                (
                    str(checkpoint.get("customer_name", "na")),
                    str(checkpoint.get("rule_name", "na")),
                    str(checkpoint.get("thread_id", "na")),
                    int(checkpoint.get("log_buffer", 0)),
                    datetime.strptime(checkpoint.get("query_schedule_time"), "%Y-%m-%d %H:%M:%S"),
                    datetime.strptime(checkpoint.get("execution_time"), "%Y-%m-%d %H:%M:%S"),
                    datetime.strptime(checkpoint.get("end_time"), "%Y-%m-%d %H:%M:%S"),
                    datetime.strptime(
                        checkpoint.get("previous_successful_checkpoint"),
                        "%Y-%m-%d %H:%M:%S",
                    ),
                    datetime.strptime(checkpoint.get("query_checkpoint_time"), "%Y-%m-%d %H:%M:%S"),
                    int(checkpoint.get("execution_time_ms", 0)),
                    str(checkpoint.get("hunt_name", "na")),
                    str(checkpoint.get("query_id", "na")),
                )
                for checkpoint in checkpoints
            ]

            ch_client.execute(
                f"""
                INSERT INTO {self.database_name}.{self.table_name} 
                (
                    customer_name, 
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
                    query_id
                ) VALUES
                """,
                data,
            )
            logger.debug(
                f"Batch checkpoint created successfully in Clickhouse table [{self.database_name}.{self.table_name}]."
            )
        except Exception as e:
            logger.error(f"Failed to create batch checkpoints: {e}", exc_info=True)

    def create_batch_checkpoint_file(
        self,
        checkpoints: List[Dict[str, Any]],
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
