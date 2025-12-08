"""
Unit tests for ClickHouseErrorHandler class.
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "src"))

from dfe_engine.clickhouse.clickhouse_errors_mapping import ClickHouseErrorHandler


class TestClickHouseErrorHandler:
    """Test cases for ClickHouseErrorHandler class."""

    def test_parse_error_code_390_table_not_found(self):
        """Test parsing of Code 390 - Table not found error."""
        error = Exception("Code: 390. DB::Exception: Table `test_table` doesn't exist.")
        result = ClickHouseErrorHandler.parse_error(error)

        assert result["code"] == 390
        assert result["type"] == "table_not_found"
        assert "Code: 390. DB::Exception: Table `test_table` doesn't exist." in result["message"]
        assert (
            result["user_message"]
            == "Table does not exist in the database. Ensure schemas are deployed first: run build-schemas then apply-schemas before attempting updates or queries. Check: 1) Table name spelling and case sensitivity, 2) Correct database selection, or 3) Schema deployment completed successfully."
        )
        assert result["actionable"] is True

    def test_parse_error_code_241_memory_limit_query(self):
        """Test parsing of Code 241 - Memory limit exceeded for query."""
        error = Exception("Code: 241. DB::Exception: Memory limit exceeded: would use 104.61 GiB")
        result = ClickHouseErrorHandler.parse_error(error)

        assert result["code"] == 241
        assert result["type"] == "memory_limit_query"
        assert (
            "Code: 241. DB::Exception: Memory limit exceeded: would use 104.61 GiB"
            in result["message"]
        )
        assert (
            "Memory limit exceeded for this specific query. Try: 1) Simplify the query with fewer JOINs or aggregations, 2) Use GROUP BY with LIMIT for large result sets, 3) Increase max_memory_usage_for_user setting, or 4) Consider using external aggregation for very large datasets."
            in result["user_message"]
        )
        assert result["actionable"] is True

    def test_parse_error_code_62_query_size_limit(self):
        """Test parsing of Code 62 - Max query size exceeded."""
        error = Exception("Code: 62. DB::Exception: Max query size exceeded")
        result = ClickHouseErrorHandler.parse_error(error)

        assert result["code"] == 62
        assert result["type"] == "query_size_limit"
        assert "Code: 62. DB::Exception: Max query size exceeded" in result["message"]
        assert (
            "Query size limit exceeded. The SQL statement is too large to process. Try: 1) Break complex queries into smaller operations, 2) Use temporary tables for intermediate results, 3) Increase max_query_size setting, or 4) Simplify the query structure."
            in result["user_message"]
        )
        assert result["actionable"] is True

    def test_parse_error_code_159_timeout_exceeded(self):
        """Test parsing of Code 159 - Timeout exceeded."""
        error = Exception("Code: 159. DB::Exception: Timeout exceeded")
        result = ClickHouseErrorHandler.parse_error(error)

        assert result["code"] == 159
        assert result["type"] == "timeout"
        assert "Code: 159. DB::Exception: Timeout exceeded" in result["message"]
        assert (
            "Query execution timed out. The operation took too long to complete. Try: 1) Add indexes on frequently filtered columns, 2) Use OPTIMIZE TABLE for better performance, 3) Increase timeout settings (max_execution_time), or 4) Break the query into smaller chunks."
            in result["user_message"]
        )
        assert result["actionable"] is True

    def test_parse_error_code_57_table_already_exists(self):
        """Test parsing of Code 57 - Table already exists."""
        error = Exception("Code: 57. DB::Exception: Table `test_table` already exists")
        result = ClickHouseErrorHandler.parse_error(error)

        assert result["code"] == 57
        assert result["type"] == "table_already_exists"
        assert "Code: 57. DB::Exception: Table `test_table` already exists" in result["message"]
        assert (
            "Table already exists in the database. If you want to recreate it, first drop the existing table with: DROP TABLE table_name. If you want to modify the schema, use ALTER TABLE instead."
            in result["user_message"]
        )
        assert result["actionable"] is True

    def test_parse_error_unknown_code(self):
        """Test parsing of unknown error code."""
        error = Exception("Code: 999. DB::Exception: Unknown error occurred")
        result = ClickHouseErrorHandler.parse_error(error)

        assert result["code"] is None
        assert result["type"] == "unknown_error"
        assert "Unknown error occurred" in result["message"]
        assert (
            "Database operation failed. Ensure schemas are deployed first using apply-schemas command before running updates. Check server logs for technical details if the issue persists."
            in result["user_message"]
        )
        assert result["actionable"] is False

    def test_parse_error_no_code(self):
        """Test parsing of error without code."""
        error = Exception("Some random error without code")
        result = ClickHouseErrorHandler.parse_error(error)

        assert result["code"] is None
        assert result["type"] == "unknown_error"
        assert "Some random error without code" in result["message"]
        assert (
            "Database operation failed. Ensure schemas are deployed first using apply-schemas command before running updates. Check server logs for technical details if the issue persists."
            in result["user_message"]
        )
        assert result["actionable"] is False

    def test_get_error_message_known_codes(self):
        """Test get_error_message for known error codes."""
        assert (
            ClickHouseErrorHandler.get_error_message(390)
            == "Table does not exist in the database. Ensure schemas are deployed first: run build-schemas then apply-schemas before attempting updates or queries. Check: 1) Table name spelling and case sensitivity, 2) Correct database selection, or 3) Schema deployment completed successfully."
        )
        assert (
            ClickHouseErrorHandler.get_error_message(241)
            == "Memory limit exceeded for this specific query. Try: 1) Simplify the query with fewer JOINs or aggregations, 2) Use GROUP BY with LIMIT for large result sets, 3) Increase max_memory_usage_for_user setting, or 4) Consider using external aggregation for very large datasets."
        )
        assert (
            ClickHouseErrorHandler.get_error_message(62)
            == "Query size limit exceeded. The SQL statement is too large to process. Try: 1) Break complex queries into smaller operations, 2) Use temporary tables for intermediate results, 3) Increase max_query_size setting, or 4) Simplify the query structure."
        )
        assert (
            ClickHouseErrorHandler.get_error_message(159)
            == "Query execution timed out. The operation took too long to complete. Try: 1) Add indexes on frequently filtered columns, 2) Use OPTIMIZE TABLE for better performance, 3) Increase timeout settings (max_execution_time), or 4) Break the query into smaller chunks."
        )
        assert (
            ClickHouseErrorHandler.get_error_message(57)
            == "Table already exists in the database. If you want to recreate it, first drop the existing table with: DROP TABLE table_name. If you want to modify the schema, use ALTER TABLE instead."
        )

    def test_get_error_type_known_codes(self):
        """Test get_error_type for known error codes."""
        assert ClickHouseErrorHandler.get_error_type(390) == "table_not_found"
        assert ClickHouseErrorHandler.get_error_type(241) == "memory_limit_query"
        assert ClickHouseErrorHandler.get_error_type(62) == "query_size_limit"
        assert ClickHouseErrorHandler.get_error_type(159) == "timeout"
        assert ClickHouseErrorHandler.get_error_type(57) == "table_already_exists"
        assert ClickHouseErrorHandler.get_error_type(999) == "unknown_error"

    def test_is_retryable_error(self):
        """Test is_retryable_error for various error codes."""
        assert ClickHouseErrorHandler.is_retryable_error(241) is True
        assert ClickHouseErrorHandler.is_retryable_error(159) is True
        assert ClickHouseErrorHandler.is_retryable_error(390) is False
        assert ClickHouseErrorHandler.is_retryable_error(57) is False
        assert ClickHouseErrorHandler.is_retryable_error(62) is False
        assert ClickHouseErrorHandler.is_retryable_error(999) is False

    def test_format_error_for_api_known_error(self):
        """Test format_error_for_api with known error."""
        error = Exception("Code: 390. DB::Exception: Table `test_table` doesn't exist.")
        result = ClickHouseErrorHandler.format_error_for_api(error)

        expected = {
            "error": {
                "code": 390,
                "type": "table_not_found",
                "message": "Table does not exist in the database. Ensure schemas are deployed first: run build-schemas then apply-schemas before attempting updates or queries. Check: 1) Table name spelling and case sensitivity, 2) Correct database selection, or 3) Schema deployment completed successfully.",
                "details": "Code: 390. DB::Exception: Table `test_table` doesn't exist.",
                "actionable": True,
            }
        }
        assert result == expected

    def test_format_error_for_api_unknown_error(self):
        """Test format_error_for_api with unknown error."""
        error = Exception("Some unknown error")
        result = ClickHouseErrorHandler.format_error_for_api(error)

        expected = {
            "error": {
                "code": None,
                "type": "unknown_error",
                "message": "Database operation failed. Ensure schemas are deployed first using apply-schemas command before running updates. Check server logs for technical details if the issue persists.",
                "details": "Some unknown error",
                "actionable": False,
            }
        }
        assert result == expected

    def test_real_error_message_parsing(self):
        """Test parsing of real ClickHouse error messages from production."""

        error_msg = (
            "Code: 390. DB::Exception: Table default.test_table doesn't exist. "
            "(UNKNOWN_TABLE) (version 23.8.2.7)"
        )

        error = Exception(error_msg)
        result = ClickHouseErrorHandler.parse_error(error)

        assert result["code"] == 390
        assert result["type"] == "table_not_found"
        assert (
            "Code: 390. DB::Exception: Table default.test_table doesn't exist." in result["message"]
        )
        assert (
            result["user_message"]
            == "Table does not exist in the database. Ensure schemas are deployed first: run build-schemas then apply-schemas before attempting updates or queries. Check: 1) Table name spelling and case sensitivity, 2) Correct database selection, or 3) Schema deployment completed successfully."
        )
        assert result["actionable"] is True

    def test_memory_limit_error_with_details(self):
        """Test parsing of memory limit error with specific details."""
        error_msg = (
            "Code: 241. DB::Exception: Memory limit (total) exceeded: "
            "would use 1.00 GiB (attempt to allocate chunk of 1048576 bytes), "
            "maximum: 1.00 GiB. (MEMORY_LIMIT_EXCEEDED) (version 23.8.2.7)"
        )

        error = Exception(error_msg)
        result = ClickHouseErrorHandler.parse_error(error)

        assert result["code"] == 241
        assert result["type"] == "memory_limit_query"
        assert "Memory limit (total) exceeded" in result["message"]
        assert "Memory limit exceeded for this specific query" in result["user_message"]
        assert result["actionable"] is True
