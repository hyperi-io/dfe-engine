import json
import datetime

import pytz
from clickhouse_connect.driver import Client
from clickhouse_connect.driver.exceptions import ClickHouseError
from hs_lib.logger import logger


class DataTool:
    @staticmethod
    def handle_none(value):
        """Replace None values with an empty string or appropriate default value."""
        return "" if value is None else value

    @staticmethod
    def parse_datetime(value):
        """Convert string or integer timestamp to datetime in ClickHouse format."""
        if isinstance(value, str):
            try:
                # Handle ISO 8601 format with timezone info or plain date-time
                if "T" in value or " " in value:
                    dt = datetime.datetime.fromisoformat(value)
                else:
                    # Attempt to parse as a date string
                    dt = datetime.datetime.strptime(value, "%Y-%m-%d")
                # Handle timezone info if present
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=pytz.UTC)
                return dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            except ValueError as e:
                logger.error(f"Failed to parse datetime string: '{value}'. Error: {e}")
            except TypeError as e:
                logger.error(f"Type error in datetime parsing: '{value}'. Error: {e}")
            except Exception as e:
                logger.error(f"Unexpected error in datetime parsing: '{value}'. Error: {e}")
        elif isinstance(value, (int, float)):  # Handle Unix timestamps
            try:
                dt = datetime.datetime.fromtimestamp(value, tz=pytz.UTC)
                return dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            except (ValueError, OSError) as e:
                logger.error(f"Failed to parse timestamp: '{value}'. Error: {e}")
            except Exception as e:
                logger.error(f"Unexpected error in timestamp parsing: '{value}'. Error: {e}")
        else:
            logger.warning(f"Unexpected value type for datetime parsing: {type(value)}")
        return None  # Return None for unhandled cases

    @staticmethod
    def ensure_type(value, expected_type):
        """Ensure the value matches the expected type."""
        if isinstance(value, expected_type):
            return value
        if expected_type is int and isinstance(value, (str, float)):
            try:
                return int(float(value))
            except ValueError:
                logger.info(f"Value conversion to int failed: {value}")
        elif expected_type is float and isinstance(value, (str, int)):
            try:
                return float(value)
            except ValueError:
                logger.info(f"Value conversion to float failed: {value}")
        return None  # Return None for unhandled cases

    @staticmethod
    def ensure_ip_address(value):
        """Ensure that IP addresses are valid."""
        return str(value) if value else ""

    @staticmethod
    def parse_and_handle_value(key, value):
        """Parse and handle specific fields like timestamps and integers."""
        timestamp_keys = {
            "timestamp",
            "timestamp_load",
            "event_creation_time",
            "event_received_time",
            "event_time",
            "previous_time",
            "sysmon_utc_time",
        }
        ip_keys = {
            "ip_address",
            "destination_ip",
            "destination_ip_ip4",
            "destination_ip_ip6",
            "destination_is_ipv6",
            "src_ip_addr",
            "src_ip_addr_v4",
            "src_ip_addr_v6",
            "source_ip_addr",
            "source_ip_addr_v4",
            "source_ip_addr_v6",
        }
        int_keys = {
            "destination_port",
            "source_port",
            "ip_port",
            "src_port",
            "src_port_number",
            "source_port_number",
            "thread_id",
            "thread_new_id",
            "logon_type",
            "level_value",
            "message_id",
            "new_process_id",
            "logon_key_length",
            "restricted_sid_count",
        }

        if key in timestamp_keys:
            return DataTool.parse_datetime(value)
        elif key in ip_keys:
            return DataTool.ensure_ip_address(value)
        elif key in int_keys:
            return DataTool.ensure_type(value, int)
        else:
            return DataTool.handle_none(value)

    @staticmethod
    def insert_fake_nxlog_windows_records(
        ch_client: Client, json_file_path: str, target_database: str, target_table: str
    ):
        """Insert records from a JSON file into ClickHouse."""
        try:
            with open(json_file_path, "r") as json_file:
                records_to_insert = []
                first_record = json.loads(json_file.readline().strip())
                column_keys = list(first_record.keys())

                # Ensure column_keys are properly formatted for the query
                column_keys = [key.replace('"', "") for key in column_keys]

                for line in json_file:
                    record = json.loads(line.strip())
                    # Prepare record values for insertion
                    record_values = [
                        DataTool.parse_and_handle_value(key, record.get(key)) for key in column_keys
                    ]

                    # Format values for SQL
                    formatted_values = []
                    for value in record_values:
                        if isinstance(value, str):
                            # Escape single quotes in string values
                            formatted_value = value.replace("'", "''")
                            formatted_values.append(f"'{formatted_value}'")
                        elif value is None:
                            formatted_values.append("''")
                        else:
                            formatted_values.append(str(value))

                    records_to_insert.append(f"({', '.join(formatted_values)})")

                # Ensure there are records to insert
                if records_to_insert:
                    # Batch insert with parameterized query
                    columns = ", ".join(column_keys)
                    values_placeholder = ", ".join(records_to_insert)
                    query = f"INSERT INTO {target_database}.{target_table} ({columns}) VALUES {values_placeholder}"

                    # Execute the query
                    ch_client.command(query)
                    logger.info(
                        f"Records inserted successfully for database {target_database} in table {target_table}."
                    )
                else:
                    logger.info("No records found to insert.")

        except (json.JSONDecodeError, ClickHouseError) as err:
            logger.error(f"Error occurred: {err}")
        except Exception as e:
            logger.error(f"An unexpected error occurred: {e}")
