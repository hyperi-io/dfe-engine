#  Project:      dfe-engine
#  File:         clickhouse/statements.py
#  Purpose:      Cut a script into statements, and size max_query_size to a DDL statement
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""What the engine reads from a statement's text before it sends it.

``split_statements`` cuts a script at the semicolons that end a statement, read
from ClickHouse's own tokens, so a ``;`` inside a string, a quoted identifier, a
heredoc or a comment stays in the statement it belongs to.

``ddl_settings`` gives a schema DDL statement the ``max_query_size`` it needs.
ClickHouse refuses to parse a query longer than its 256 KiB default, and the
generated table for a wide vendor schema runs to several times that. Only the
schema DDL paths send it; a caller's own SQL stays on the server's limit.
"""

from sqlglot.dialects.dialect import Dialect
from sqlglot.errors import SqlglotError
from sqlglot.tokens import TokenType

_CLICKHOUSE = Dialect.get_or_raise("clickhouse")

# ClickHouse's compiled-in max_query_size; a statement within it is sent without the setting.
CLICKHOUSE_DEFAULT_MAX_QUERY_SIZE = 262_144

# Headroom over the statement's own bytes, so a statement measured at the limit still parses.
MAX_QUERY_SIZE_MARGIN = 4096


class StatementTooLargeError(ValueError):
    """A DDL statement needs a larger ``max_query_size`` than the deployment allows."""


def split_statements(script: str) -> list[str]:
    """Cut *script* into its statements at the semicolons that end one.

    Args:
        script: One statement or several, with or without a trailing ``;``.

    Returns:
        Each statement, stripped, in order; empty for a blank script. Text the
        tokenizer cannot read, an unterminated quote say, comes back whole for
        ClickHouse to refuse with its own message.
    """
    if ";" not in script:
        whole = script.strip()
        return [whole] if whole else []
    try:
        tokens = _CLICKHOUSE.tokenize(script)
    # sqlglot raises AttributeError and RecursionError on some input it cannot read.
    except SqlglotError, AttributeError, RecursionError:
        whole = script.strip().rstrip(";").strip()
        return [whole] if whole else []
    pieces = []
    start = 0
    for token in tokens:
        if token.token_type is TokenType.SEMICOLON:
            pieces.append(script[start : token.start])
            start = token.end + 1
    pieces.append(script[start:])
    return [piece.strip() for piece in pieces if piece.strip()]


def ddl_settings(statement: str, *, ceiling: int | None = None) -> dict[str, int]:
    """The settings that let ClickHouse parse *statement* whole.

    A statement within ClickHouse's default gets none, so a server whose profile
    raised the limit keeps its own value. A larger one gets its own byte length
    plus a margin, sized per statement rather than one large constant for all.

    Args:
        statement: The DDL statement about to be sent.
        ceiling: The largest ``max_query_size`` to send. None reads
            ``clickhouse.ddl_max_query_size``.

    Returns:
        ``{"max_query_size": n}``, or an empty dict.

    Raises:
        StatementTooLargeError: The statement needs more than ``ceiling``.
    """
    size = len(statement.encode("utf-8"))
    needed = size + MAX_QUERY_SIZE_MARGIN
    if needed <= CLICKHOUSE_DEFAULT_MAX_QUERY_SIZE:
        return {}
    if ceiling is None:
        from dfe_engine.settings import get_settings

        ceiling = get_settings().clickhouse.ddl_max_query_size
    if needed > ceiling:
        raise StatementTooLargeError(
            f"the DDL statement is {size} bytes, more than the {ceiling}-byte "
            "max_query_size ceiling allows (DFE_CLICKHOUSE_DDL_MAX_QUERY_SIZE)"
        )
    return {"max_query_size": needed}
