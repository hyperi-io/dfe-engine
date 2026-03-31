"""Tests for ClickHouse error handler — pure logic, no CH connection."""

from __future__ import annotations

from dfe_engine.clickhouse.clickhouse_errors_mapping import ClickHouseErrorHandler


class TestParseError:
    def test_known_error_code(self):
        exc = Exception("Code: 36. DB::Exception: Unknown table 'foo'")
        result = ClickHouseErrorHandler.parse_error(exc)
        assert result["code"] == 36
        assert result["type"] == "unknown_table"
        assert result["category"] == "syntax"
        assert result["is_actionable"] is True

    def test_database_not_found_by_text(self):
        exc = Exception("Database 'mydb' does not exist")
        result = ClickHouseErrorHandler.parse_error(exc)
        assert result["code"] == 81
        assert result["type"] == "database_not_found"

    def test_unknown_error(self):
        exc = Exception("Something totally unexpected")
        result = ClickHouseErrorHandler.parse_error(exc)
        assert result["code"] is None
        assert result["type"] == "unknown_error"
        assert result["is_actionable"] is False

    def test_memory_limit_error(self):
        exc = Exception("Code: 60. Memory limit exceeded")
        result = ClickHouseErrorHandler.parse_error(exc)
        assert result["code"] == 60
        assert result["type"] == "memory_limit"
        assert result["category"] == "resource_limit"

    def test_access_denied(self):
        exc = Exception("Code: 192. Access denied for user")
        result = ClickHouseErrorHandler.parse_error(exc)
        assert result["type"] == "access_denied"
        assert result["category"] == "permission"

    def test_timeout(self):
        exc = Exception("Code: 159. Query timed out")
        result = ClickHouseErrorHandler.parse_error(exc)
        assert result["type"] == "timeout"

    def test_authentication_failed(self):
        exc = Exception("Code: 193. Authentication failed")
        result = ClickHouseErrorHandler.parse_error(exc)
        assert result["type"] == "authentication_failed"

    def test_connection_timeout(self):
        exc = Exception("Code: 209. Connection timed out")
        result = ClickHouseErrorHandler.parse_error(exc)
        assert result["type"] == "connection_timeout"
        assert result["category"] == "connection"


class TestGetErrorMessage:
    def test_known_code(self):
        msg = ClickHouseErrorHandler.get_error_message(36)
        assert msg is not None
        assert "Unknown table" in msg

    def test_unknown_code(self):
        msg = ClickHouseErrorHandler.get_error_message(99999)
        assert msg is None


class TestGetErrorType:
    def test_known_code(self):
        assert ClickHouseErrorHandler.get_error_type(36) == "unknown_table"

    def test_unknown_code(self):
        assert ClickHouseErrorHandler.get_error_type(99999) == "unknown_error"


class TestGetErrorCategory:
    def test_known_code(self):
        assert ClickHouseErrorHandler.get_error_category(60) == "resource_limit"

    def test_unknown_code(self):
        assert ClickHouseErrorHandler.get_error_category(99999) is None


class TestIsResourceLimitError:
    def test_resource_limit(self):
        assert ClickHouseErrorHandler.is_resource_limit_error(60) is True

    def test_not_resource_limit(self):
        assert ClickHouseErrorHandler.is_resource_limit_error(36) is False


class TestIsRetryableError:
    def test_timeout_is_retryable(self):
        assert ClickHouseErrorHandler.is_retryable_error(159) is True

    def test_connection_is_retryable(self):
        assert ClickHouseErrorHandler.is_retryable_error(209) is True

    def test_concurrency_is_retryable(self):
        assert ClickHouseErrorHandler.is_retryable_error(173) is True

    def test_syntax_not_retryable(self):
        assert ClickHouseErrorHandler.is_retryable_error(36) is False


class TestFormatErrorForApi:
    def test_known_error(self):
        exc = Exception("Code: 36. Unknown table")
        result = ClickHouseErrorHandler.format_error_for_api(exc)
        assert "error" in result
        assert result["error"]["code"] == 36
        assert result["error"]["type"] == "unknown_table"
        assert result["error"]["is_actionable"] is True

    def test_unknown_error(self):
        exc = Exception("Random failure")
        result = ClickHouseErrorHandler.format_error_for_api(exc)
        assert result["error"]["code"] is None
        assert result["error"]["is_actionable"] is False
