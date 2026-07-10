#  Project:      dfe-engine
#  File:         tests/unit/test_clickhouse/test_errors.py
#  Purpose:      ClickHouse error taxonomy - classify + retry-decision classifiers
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for the CH error taxonomy - pure logic, no CH connection.

These classifiers are the SSoT the CH manager injects into scalo's
:class:`~scalo.resilience.ReconnectingResilience` as ``is_transient`` /
``is_reconnectable``: they decide connection-outage (reconnect) vs rate-limit
(back off, no reconnect) vs genuine query error (surface immediately).
"""

from __future__ import annotations

from dfe_engine.clickhouse.errors import (
    ChError,
    ErrorCategory,
    classify,
    is_connection_error,
    is_retryable_error,
    parse_code,
    wrap_ch_error,
)


def _exc(code: int, tail: str = "message") -> Exception:
    """A driver-style error string carrying a CH ``Code: NNN`` (what the driver emits)."""
    return Exception(f"Code: {code}. DB::Exception: {tail}")


class TestParseCode:
    def test_parses_code(self):
        assert parse_code(_exc(60, "Unknown table")) == 60

    def test_none_when_absent(self):
        assert parse_code(Exception("no code here")) is None


class TestClassify:
    def test_connection_codes(self):
        assert classify(_exc(209)) is ErrorCategory.CONNECTION  # SOCKET_TIMEOUT
        assert classify(_exc(210)) is ErrorCategory.CONNECTION  # NETWORK_ERROR

    def test_rate_limited_code(self):
        assert classify(_exc(202)) is ErrorCategory.RATE_LIMITED  # TOO_MANY_SIMULTANEOUS_QUERIES

    def test_query_logic_codes(self):
        assert classify(_exc(62)) is ErrorCategory.USER_ERROR  # SYNTAX_ERROR
        assert classify(_exc(60)) is ErrorCategory.NOT_FOUND  # UNKNOWN_TABLE
        assert classify(_exc(241)) is ErrorCategory.RESOURCE_LIMIT  # MEMORY_LIMIT_EXCEEDED
        assert classify(_exc(159)) is ErrorCategory.TIMEOUT  # TIMEOUT_EXCEEDED

    def test_python_builtin_connection_errors(self):
        assert classify(ConnectionError("connection refused")) is ErrorCategory.CONNECTION
        assert classify(TimeoutError("timed out")) is ErrorCategory.CONNECTION

    def test_raw_transport_text_without_code(self):
        # A urllib3 / OS socket error carries no "Code: NNN" - match the keywords.
        assert classify(Exception("Connection refused")) is ErrorCategory.CONNECTION
        assert classify(Exception("Max retries exceeded")) is ErrorCategory.CONNECTION

    def test_unknown_without_code_or_keyword(self):
        assert classify(Exception("something odd happened")) is ErrorCategory.UNKNOWN

    def test_unmapped_code_is_server(self):
        assert classify(_exc(999999)) is ErrorCategory.SERVER


class TestIsRetryableError:
    def test_connection_is_retryable(self):
        assert is_retryable_error(_exc(210)) is True

    def test_rate_limit_is_retryable(self):
        assert is_retryable_error(_exc(202)) is True

    def test_syntax_not_retryable(self):
        assert is_retryable_error(_exc(62)) is False

    def test_memory_limit_not_retryable(self):
        assert is_retryable_error(_exc(241)) is False

    def test_exec_timeout_not_retryable(self):
        # 159 TIMEOUT_EXCEEDED is a query-exec timeout, NOT a transport timeout.
        assert is_retryable_error(_exc(159)) is False


class TestIsConnectionError:
    def test_connection_true(self):
        assert is_connection_error(_exc(209)) is True

    def test_rate_limit_is_not_a_connection_error(self):
        # RATE_LIMITED is retryable but must NOT reconnect (the connection is fine).
        assert is_connection_error(_exc(202)) is False

    def test_query_error_is_not_a_connection_error(self):
        assert is_connection_error(_exc(62)) is False


class TestWrapChError:
    def test_wrap_carries_code_and_category(self):
        wrapped = wrap_ch_error(_exc(60, "Unknown table 'x'"))
        assert isinstance(wrapped, ChError)
        assert wrapped.code == 60
        assert wrapped.category is ErrorCategory.NOT_FOUND
        assert wrapped.user_safe is True

    def test_wrap_is_idempotent(self):
        original = wrap_ch_error(_exc(62))
        assert wrap_ch_error(original) is original

    def test_server_error_not_user_safe(self):
        wrapped = wrap_ch_error(Exception("Connection refused"))
        assert wrapped.category is ErrorCategory.CONNECTION
        assert wrapped.user_safe is False
