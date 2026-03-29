#  Project:      dfe-engine
#  File:         clickhouse_manager.py
#  Purpose:      ClickHouse connection management using clickhouse-connect
#  Language:     Python
#
#  License:      FSL-1.1-ALv2
#  Copyright:    (c) 2025 HYPERI PTY LIMITED

"""
ClickHouse connection management using clickhouse-connect.

Uses the official ClickHouse Inc. driver with built-in HTTP connection pooling.
"""

from threading import Lock
from typing import Annotated

import clickhouse_connect
from clickhouse_connect.driver import Client, httputil
from hyperi_pylib.logger import logger

from ..settings import get_settings


class ClickHouseClientWrapper:
    """
    Wrapper around clickhouse-connect Client to provide backward-compatible execute() method.

    clickhouse-connect uses:
    - command() for DDL/DML statements (CREATE, DROP, ALTER, INSERT without data)
    - query() for SELECT statements that return data

    This wrapper provides execute() that auto-routes to the appropriate method.
    """

    def __init__(self, client: Client):
        self._client = client

    def execute(self, query: str, *args, **kwargs):
        """
        Execute a query, routing to command() or query() based on query type.

        For backward compatibility with clickhouse-driver style code.
        Handles multi-statement queries by splitting on semicolons.
        """
        # Strip trailing semicolons and whitespace
        query = query.strip().rstrip(";").strip()

        # Check if this is a multi-statement query
        # Simple heuristic: if there's a semicolon not inside quotes, split
        if ";" in query:
            # Split and execute each statement
            statements = [s.strip() for s in query.split(";") if s.strip()]
            result = []
            for stmt in statements:
                result += self._execute_single(stmt, *args, **kwargs)
            return result

        return self._execute_single(query, *args, **kwargs)

    def _execute_single(self, query: str, *args, **kwargs):
        """Execute a single query statement."""
        query_upper = query.strip().upper()

        # DESCRIBE, DESC, EXISTS, EXPLAIN, SHOW return data - use query()
        if query_upper.startswith(("DESCRIBE", "DESC", "EXISTS", "EXPLAIN", "SHOW")):
            return self._client.query(query, *args, **kwargs).result_rows

        # INSERT with data - use insert() method
        # clickhouse-driver style: execute("INSERT INTO table (cols) VALUES", [(data, ...)])
        if query_upper.startswith("INSERT") and args and isinstance(args[0], (list, tuple)):
            return self._handle_insert_with_data(query, args[0])

        # DDL/DML commands that don't return data go to command()
        if query_upper.startswith(
            (
                "CREATE",
                "DROP",
                "ALTER",
                "TRUNCATE",
                "RENAME",
                "INSERT",
                "DELETE",
                "UPDATE",
                "SET",
                "USE",
                "GRANT",
                "REVOKE",
                "ATTACH",
                "DETACH",
                "OPTIMIZE",
                "EXCHANGE",
                "SYSTEM",
                "CHECK",
                "KILL",
            )
        ):
            return self._client.command(query, *args, **kwargs)

        # SELECT queries return data
        return self._client.query(query, *args, **kwargs).result_rows

    def _handle_insert_with_data(self, query: str, data: list):
        """Handle INSERT statements with data using clickhouse-connect's insert() method.

        Converts clickhouse-driver style INSERT calls to clickhouse-connect format.
        """
        import re

        # Parse INSERT INTO table (columns) VALUES
        # Pattern: INSERT INTO [db.]table (col1, col2, ...) VALUES
        pattern = r"INSERT\s+INTO\s+([^\s(]+)\s*\(\s*([^)]+)\s*\)\s*VALUES"
        match = re.search(pattern, query, re.IGNORECASE)

        if not match:
            # Fallback: try to execute as raw query (may fail)
            raise ValueError(f"Cannot parse INSERT query for data insertion: {query[:100]}...")

        table_name = match.group(1)
        columns_str = match.group(2)
        column_names = [c.strip() for c in columns_str.split(",")]

        # Use clickhouse-connect's insert() method
        return self._client.insert(table_name, data, column_names=column_names)


class ClickHouseManager:
    """
    Manages ClickHouse connections using clickhouse-connect.

    Uses built-in HTTP connection pooling via urllib3.
    Pool configuration is managed via settings.clickhouse.connections_max.
    """

    _instance = None  # Singleton instance

    def __init__(self, target_config_data: dict | None = None):
        self.lock = Lock()
        self.target_config_data = target_config_data or {}
        settings = get_settings()
        self.connections_max = settings.clickhouse.connections_max
        self._client: Client | None = None
        self._pool_manager = None

    @classmethod
    def get_instance(cls, target_config_data: dict | None = None):
        if cls._instance is None:
            cls._instance = cls(target_config_data)
        return cls._instance

    @classmethod
    def reset_instance(cls):
        """Reset the singleton instance (useful for testing)."""
        if cls._instance is not None:
            cls._instance._cleanup()
            cls._instance = None

    def get_clickhouse_client(self) -> ClickHouseClientWrapper:
        """Get a ClickHouse client with connection pooling.

        Returns a wrapper that provides backward-compatible execute() method.
        """
        try:
            if self._client is None:
                self._initialize_client()
            return ClickHouseClientWrapper(self._client)

        except Exception as e:
            logger.error(f"An unexpected error occurred during client acquisition: {e}")
            raise

    def _initialize_client(self):
        """Initialize the ClickHouse client with connection pooling."""
        try:
            host = self.target_config_data.get("ch_host", "localhost")
            port = self.target_config_data.get("ch_port", 8123)
            user = self.target_config_data.get("ch_username")
            password = self.target_config_data.get("ch_password")
            secure = self.target_config_data.get("ch_secure", True)
            verify = self.target_config_data.get("ch_verify", False)

            is_password_set = password is not None

            logger.info(
                "Initializing ClickHouse client",
                user=user,
                host=host,
                port=port,
                secure=secure,
                password_set=is_password_set,
            )

            # Create a custom pool manager for connection pooling
            # clickhouse-connect uses urllib3 under the hood
            self._pool_manager = httputil.get_pool_manager(
                maxsize=self.connections_max,
                num_pools=10,
            )

            # Build connection parameters
            connect_params = {
                "host": host,
                "port": port,
                "pool_mgr": self._pool_manager,
            }

            # Add authentication if provided
            if user is not None:
                connect_params["username"] = user
            if password is not None:
                connect_params["password"] = password

            # Configure HTTPS
            if secure:
                connect_params["secure"] = True
                connect_params["verify"] = verify

            if host == "localhost" and (user is not None or password is not None):
                logger.warning(
                    "Connecting to localhost with authentication. "
                    "Ensure your local cluster has proper auth configured."
                )

            # Log connection params (without password)
            log_params = {k: v for k, v in connect_params.items() if k != "password"}
            log_params.pop("pool_mgr", None)  # Don't log pool manager object
            logger.info(f"Creating clickhouse-connect client with: {log_params}")

            with self.lock:
                self._client = clickhouse_connect.get_client(**connect_params)

            logger.info("ClickHouse client initialized successfully")

        except Exception as e:
            logger.error(f"Failed to initialize ClickHouse client: {e}")
            if host == "localhost":
                logger.warning(
                    "If using localhost, ensure ClickHouse is running and accessible. "
                    "For local dev without auth, do not set username/password in targets file."
                )
            raise

    def _cleanup(self):
        """Clean up the ClickHouse client and pool."""
        try:
            if self._client is not None:
                self._client.close()
                self._client = None
                logger.info("ClickHouse client closed successfully.")
            if self._pool_manager is not None:
                self._pool_manager.clear()
                self._pool_manager = None
                logger.info("ClickHouse connection pool cleared successfully.")
        except Exception as e:
            logger.error(f"Failed to cleanup ClickHouse client: {e}")

    def teardown_test_databases(self, test_databases: Annotated[list[str], "min_length = 1"]):
        """Drop test databases."""
        client = self.get_clickhouse_client()
        try:
            for db in test_databases:
                logger.info(f"Dropping database {db}")
                client.execute(f"DROP DATABASE IF EXISTS {db}")
            logger.info("All test databases dropped successfully.")
        except Exception as e:
            logger.error(f"Failed to drop test databases: {e}")
