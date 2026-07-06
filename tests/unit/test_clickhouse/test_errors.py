#  Project:      dfe-engine
#  File:         tests/unit/test_clickhouse/test_errors.py
#  Purpose:      ClickHouse error taxonomy - classify + retry SSoT + wrap
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Error taxonomy: classification, the retry SSoT, and typed wrapping."""

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


def test_parse_code():
    assert parse_code(Exception("Code: 202. blah")) == 202
    assert parse_code(Exception("no code here")) is None


class TestClassify:
    def test_builtin_connection_errors(self):
        assert classify(ConnectionError("refused")) is ErrorCategory.CONNECTION
        assert classify(TimeoutError("t")) is ErrorCategory.CONNECTION

    def test_transport_keywords_without_a_ch_code(self):
        assert classify(Exception("read timed out")) is ErrorCategory.CONNECTION
        assert classify(Exception("Max retries exceeded")) is ErrorCategory.CONNECTION

    def test_ch_codes(self):
        assert classify(Exception("Code: 209. socket")) is ErrorCategory.CONNECTION
        assert classify(Exception("Code: 210. network")) is ErrorCategory.CONNECTION
        assert classify(Exception("Code: 202. too many")) is ErrorCategory.RATE_LIMITED
        assert classify(Exception("Code: 241. memory")) is ErrorCategory.RESOURCE_LIMIT
        assert classify(Exception("Code: 159. timeout")) is ErrorCategory.TIMEOUT
        assert classify(Exception("Code: 62. syntax")) is ErrorCategory.USER_ERROR
        assert classify(Exception("Code: 193. auth")) is ErrorCategory.ACCESS

    def test_unknown_code_is_server(self):
        assert classify(Exception("Code: 999. weird")) is ErrorCategory.SERVER

    def test_no_code_no_keyword_is_unknown(self):
        assert classify(Exception("something odd")) is ErrorCategory.UNKNOWN


class TestRetryDecision:
    def test_connection_and_rate_limited_are_retryable(self):
        assert is_retryable_error(Exception("Code: 210. network"))
        assert is_retryable_error(Exception("Code: 202. too many"))
        assert is_retryable_error(ConnectionError("refused"))

    def test_memory_timeout_syntax_are_not_retryable(self):
        assert not is_retryable_error(Exception("Code: 241. memory"))
        assert not is_retryable_error(Exception("Code: 159. timeout"))
        assert not is_retryable_error(Exception("Code: 160. too slow"))
        assert not is_retryable_error(Exception("Code: 62. syntax"))

    def test_only_connection_is_a_connection_error(self):
        assert is_connection_error(Exception("Code: 210. network"))
        # rate-limited (202) is retryable but NOT a connection outage (no reconnect)
        assert not is_connection_error(Exception("Code: 202. too many"))


class TestWrap:
    def test_wraps_with_code_category_user_safe(self):
        err = wrap_ch_error(Exception("Code: 60. Unknown table foo"))
        assert isinstance(err, ChError)
        assert err.code == 60
        assert err.category is ErrorCategory.NOT_FOUND
        assert err.user_safe is True

    def test_server_error_is_not_user_safe(self):
        err = wrap_ch_error(Exception("Code: 210. network"))
        assert err.category is ErrorCategory.CONNECTION
        assert err.user_safe is False

    def test_idempotent(self):
        err = wrap_ch_error(Exception("Code: 62. syntax error"))
        assert wrap_ch_error(err) is err
