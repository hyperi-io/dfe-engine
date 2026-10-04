#  Project:      dfe-engine
#  File:         clickhouse/errors.py
#  Purpose:      ClickHouse error taxonomy - typed exceptions + classification
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""ClickHouse error taxonomy - the SSoT for classifying + retrying CH errors.

Two concerns, both needed (pattern adapted from PostHog, MIT - see
THIRD-PARTY-NOTICES):
  - :func:`classify` maps a raw driver error to an :class:`ErrorCategory` (the
    coarse label for metrics AND the retry decision - this is what the resilience
    layer keys off);
  - :func:`wrap_ch_error` turns a raw driver error into a typed :class:`ChError`
    carrying the CH code + category + ``user_safe``, so callers branch meaningfully
    and the raw driver text is never assumed safe to show a user.

Retry SSoT (plan 13.1, verified against the driver docs): retry with backoff on
202 TOO_MANY_SIMULTANEOUS_QUERIES and the connection codes 209 SOCKET_TIMEOUT /
210 NETWORK_ERROR (+ the driver's own HTTP 429/503/504). NEVER retry 241
MEMORY_LIMIT_EXCEEDED, 159 TIMEOUT_EXCEEDED, 160 TOO_SLOW, or any query-logic
error - a retry is futile and wastes the budget. Branch on ``code``, never on
message strings. Code 517 (ON CLUSTER replica-metadata lag) is retried only on the
DDL/migration path, not here.

Superseded and folded in the old ``clickhouse_errors_mapping`` (now removed): its
coarse code->message table only ever served the resilience retry decision, which
this taxonomy now owns.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

_CODE_RE = re.compile(r"Code:\s*(\d+)")

# Transport-level connection markers for a raw driver/socket error carrying no
# ClickHouse "Code: NNN" (urllib3 / OS socket text). CONNECTION-only, never a
# query-level word, so a real query error is not mistaken for an outage.
_CONN_KEYWORDS = (
    "connection refused",
    "connection reset",
    "connection aborted",
    "connection error",
    "connectionerror",
    "timed out",
    "timeout",
    "unreachable",
    "temporarily unavailable",
    "max retries",
    "failed to establish",
    "name or service not known",
    "broken pipe",
    "server disconnected",
    "remote end closed",
    "cannot connect",
)


class ErrorCategory(StrEnum):
    """Coarse category for metrics + the retry decision."""

    USER_ERROR = "user_error"  # syntax / unknown identifier / type mismatch
    NOT_FOUND = "not_found"  # unknown db / table
    EXISTS = "exists"  # already exists
    ACCESS = "access"  # auth / permission / readonly
    RATE_LIMITED = "rate_limited"  # too many simultaneous queries (retryable)
    TIMEOUT = "timeout"  # exec timeout / too slow (NOT retryable)
    RESOURCE_LIMIT = "resource_limit"  # memory / query size (NOT retryable)
    CONNECTION = "connection"  # socket / network (retryable, reconnect)
    CANCELLED = "cancelled"  # query cancelled
    SERVER = "server"  # other server-side failure
    UNKNOWN = "unknown"  # no CH code parsed


@dataclass(frozen=True, slots=True)
class _Meta:
    category: ErrorCategory
    user_safe: bool = False


# Curated subset of ClickHouse ErrorCodes.cpp - the codes we categorise/act on.
# user_safe=True means the CH message is a user's own fault (safe to surface).
_CODES: dict[int, _Meta] = {
    36: _Meta(ErrorCategory.USER_ERROR, user_safe=True),  # BAD_ARGUMENTS
    43: _Meta(ErrorCategory.USER_ERROR, user_safe=True),  # ILLEGAL_TYPE_OF_ARGUMENT
    47: _Meta(ErrorCategory.USER_ERROR, user_safe=True),  # UNKNOWN_IDENTIFIER
    53: _Meta(ErrorCategory.USER_ERROR, user_safe=True),  # TYPE_MISMATCH
    57: _Meta(ErrorCategory.EXISTS, user_safe=True),  # TABLE_ALREADY_EXISTS
    60: _Meta(ErrorCategory.NOT_FOUND, user_safe=True),  # UNKNOWN_TABLE
    62: _Meta(ErrorCategory.USER_ERROR, user_safe=True),  # SYNTAX_ERROR
    81: _Meta(ErrorCategory.NOT_FOUND, user_safe=True),  # UNKNOWN_DATABASE
    82: _Meta(ErrorCategory.EXISTS, user_safe=True),  # DATABASE_ALREADY_EXISTS
    158: _Meta(ErrorCategory.RESOURCE_LIMIT, user_safe=True),  # TOO_MANY_ROWS
    159: _Meta(ErrorCategory.TIMEOUT, user_safe=True),  # TIMEOUT_EXCEEDED
    160: _Meta(ErrorCategory.TIMEOUT, user_safe=True),  # TOO_SLOW
    164: _Meta(ErrorCategory.ACCESS, user_safe=True),  # READONLY
    192: _Meta(ErrorCategory.ACCESS, user_safe=True),  # UNKNOWN_USER
    193: _Meta(ErrorCategory.ACCESS, user_safe=True),  # WRONG_PASSWORD
    202: _Meta(ErrorCategory.RATE_LIMITED, user_safe=True),  # TOO_MANY_SIMULTANEOUS_QUERIES
    209: _Meta(ErrorCategory.CONNECTION),  # SOCKET_TIMEOUT
    210: _Meta(ErrorCategory.CONNECTION),  # NETWORK_ERROR
    241: _Meta(ErrorCategory.RESOURCE_LIMIT, user_safe=True),  # MEMORY_LIMIT_EXCEEDED
    242: _Meta(ErrorCategory.ACCESS, user_safe=True),  # TABLE_IS_READ_ONLY
    394: _Meta(ErrorCategory.CANCELLED, user_safe=True),  # QUERY_WAS_CANCELLED
    439: _Meta(ErrorCategory.RATE_LIMITED),  # CANNOT_SCHEDULE_TASK
    497: _Meta(ErrorCategory.ACCESS, user_safe=True),  # ACCESS_DENIED
    516: _Meta(ErrorCategory.ACCESS, user_safe=True),  # AUTHENTICATION_FAILED
}

# The retry SSoT - categories a backoff retry can help. CONNECTION also reconnects.
_RETRYABLE_CATEGORIES = frozenset({ErrorCategory.CONNECTION, ErrorCategory.RATE_LIMITED})

# ClickHouse refused the connecting user itself: UNKNOWN_USER, WRONG_PASSWORD, AUTHENTICATION_FAILED.
_IDENTITY_CODES = frozenset({192, 193, 516})


class ChError(Exception):
    """A typed ClickHouse error - carries the CH ``code``, category + ``user_safe``.

    ``user_safe`` marks a caller-fault error whose message is safe to surface; a
    server/connection error is not (do not leak internals). Callers catch ChError
    (or check ``.category``) instead of parsing driver text.
    """

    def __init__(
        self,
        message: str,
        *,
        code: int | None = None,
        category: ErrorCategory = ErrorCategory.UNKNOWN,
        user_safe: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.category = category
        self.user_safe = user_safe


def parse_code(exc: BaseException) -> int | None:
    """Extract the ClickHouse ``Code: NNN`` from a driver error, or None."""
    match = _CODE_RE.search(str(exc))
    return int(match.group(1)) if match else None


def classify(exc: BaseException) -> ErrorCategory:
    """The coarse :class:`ErrorCategory` for *exc* (for metrics + retry).

    A Python builtin ``ConnectionError`` / ``TimeoutError`` and the driver's
    transport ``OperationalError`` are CONNECTION; otherwise the parsed CH code
    decides; otherwise UNKNOWN.
    """
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return ErrorCategory.CONNECTION
    try:
        from clickhouse_connect.driver.exceptions import OperationalError

        if isinstance(exc, OperationalError):
            return ErrorCategory.CONNECTION
    except ImportError:  # pragma: no cover - driver always importable in practice
        pass
    code = parse_code(exc)
    if code is not None:
        meta = _CODES.get(code)
        return meta.category if meta else ErrorCategory.SERVER
    # No CH code: a raw transport error - match the connection keywords.
    msg = str(exc).lower()
    if any(keyword in msg for keyword in _CONN_KEYWORDS):
        return ErrorCategory.CONNECTION
    return ErrorCategory.UNKNOWN


def is_retryable_error(exc: BaseException) -> bool:
    """True when a backoff retry may help - CONNECTION or RATE_LIMITED only.

    Excludes 241/159/160 + query-logic errors (a retry is futile). This is the
    ``is_transient`` classifier the ClickHouse manager injects into scalo's
    :class:`~scalo.resilience.ReconnectingResilience`.
    """
    return classify(exc) in _RETRYABLE_CATEGORIES


def is_connection_error(exc: BaseException) -> bool:
    """True when *exc* is a CONNECTION outage (worth a reconnect, not just backoff)."""
    return classify(exc) is ErrorCategory.CONNECTION


def is_identity_error(exc: BaseException) -> bool:
    """True when ClickHouse refused the connecting user: unknown, or the wrong password.

    Not in the resilience retry set: for most callers a refused login is
    configuration. A worker whose user the engine mints treats it as the user not
    being there yet.
    """
    return parse_code(exc) in _IDENTITY_CODES


def wrap_ch_error(exc: BaseException) -> ChError:
    """Wrap a raw driver error in a typed :class:`ChError` (idempotent)."""
    if isinstance(exc, ChError):
        return exc
    code = parse_code(exc)
    meta = _CODES.get(code) if code is not None else None
    category = meta.category if meta else classify(exc)
    return ChError(
        str(exc),
        code=code,
        category=category,
        user_safe=meta.user_safe if meta else False,
    )
