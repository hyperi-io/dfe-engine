import os
import logging
from threading import Lock
from clickhouse_pool import ChPool


class ConfigurationError(Exception):
    """Custom exception for configuration errors."""

    pass


class ClickHouseManager:
    _instance = None  # Singleton instance

    @classmethod
    def get_instance(cls, logger: logging.Logger, target_config_data: dict = None):
        if cls._instance is None:
            cls._instance = cls(logger, target_config_data)
        return cls._instance

    def __init__(self, logger: logging.Logger, target_config_data: dict = None):
        self.logger = logger
        self.lock = Lock()  # Lock for thread safety
        self.target_config_data = target_config_data
        self.connections_min = int(os.environ.get("CH_CONNECTIONS_MIN", 10))
        self.connections_max = int(os.environ.get("CH_CONNECTIONS_MAX", 300))
        self.pool = None

    def get_clickhouse_client(self):
        try:
            if self.pool is None or self.pool.closed:
                self._initialize_pool()

            with self.lock:
                with self.pool.get_client() as client:
                    return client

        except ConfigurationError as ce:
            self.logger.error(
                f"Configuration error: {ce}", exc_info=True, stack_info=True
            )
            raise
        except Exception as e:
            self.logger.error(
                f"An unexpected error occurred during client acquisition: {e}"
            )
            raise

    def _initialize_pool(self):
        try:
            host = self.target_config_data.get("ch_host", None)
            port = self.target_config_data.get("ch_port", None)
            user = self.target_config_data.get(
                "ch_username", None
            )  # Default to None if not provided
            password = self.target_config_data.get("ch_password", None)
            secure = self.target_config_data.get(
                "ch_secure", True
            )  # Default Setting we don't want the user to touch.
            verify = self.target_config_data.get(
                "ch_verify", False
            )  # Default Setting we don't want the user to touch.

            is_password_set = password is not None

            self.logger.info(
                f"Passed in Parameters for user=[{user}] on host[{host}] and port {port} is password set [{is_password_set}]"
            )

            connection_params = {"host": host, "port": port}
            if user is not None and password is not None:
                connection_params["user"] = user
                connection_params["password"] = password
                connection_params["port"] = (
                    port  # only use supplied port for clickhouse on minikube
                )
                connection_params["secure"] = secure
                connection_params["verify"] = verify

            if host == "localhost" and (user is not None or password is not None):
                self.logger.warning(
                    "Ensure you have a local cluster that has the correct user auth setup or the connection will fail."
                )

            if user is not None and password is not None:
                connection_params_no_password = connection_params.copy()
                del connection_params_no_password["password"]
                self.logger.info(
                    f"intialising cloud service or auth service connection with {connection_params_no_password}"
                )
                self.pool = ChPool(
                    connections_min=self.connections_min,
                    connections_max=self.connections_max,
                    **connection_params,
                )
            else:
                self.logger.info(
                    f"intialising local host connection with {connection_params}"
                )
                self.pool = ChPool(
                    host=host,
                    connections_min=self.connections_min,
                    connections_max=self.connections_max,
                )

        except Exception as e:
            self.logger.warning(
                f"If you have a localhost running it assumes there is no Auth needed, do not set username and password in the targets file: {e}"
            )
            self.logger.error(f"Failed to initialize ClickHouse connection pool: {e}")
            raise

    def cleanup(self):
        try:
            if self.pool and not self.pool.closed:
                # Check if the pool exists and is not closed
                self.pool.cleanup()
                self.logger.info("ClickHouse connection pool cleaned up successfully.")
        except Exception as e:
            self.logger.error(f"Failed to cleanup ClickHouse connection pool: {e}")

    def teardown_test_databases(self, test_databases):
        client = self.get_clickhouse_client()
        try:
            for db in test_databases:
                self.logger.info(f"Dropping database {db}")
                client.execute(f"DROP DATABASE IF EXISTS {db}")
            self.logger.info("All test databases dropped successfully.")
        except Exception as e:
            self.logger.error(f"Failed to drop test databases: {e}")
