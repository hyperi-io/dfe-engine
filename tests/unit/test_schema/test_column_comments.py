#  Project:      dfe-engine
#  File:         tests/unit/test_schema/test_column_comments.py
#  Purpose:      read_table returns each column's declared COMMENT, unescaped
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The comment the applier compares with ``system.columns`` is the text the CREATE declares.

ClickHouse stores the unescaped text, so a comment holding a quote or a backslash
has to read back as the same text it was rendered from.
"""

import pytest

from dfe_engine.clickhouse.quoting import quote_literal
from dfe_engine.schema.applier import read_table

TEXTS = [
    pytest.param("@source: timestamp | now()", id="directive"),
    pytest.param("it's", id="single-quote"),
    pytest.param("back\\slash", id="backslash"),
    pytest.param("line one\nline two", id="newline"),
    pytest.param("a \\' b '' c", id="quote-and-backslash-together"),
    pytest.param("{braces} (parens) [brackets]", id="brackets"),
]


@pytest.mark.parametrize("text", TEXTS)
def test_a_declared_comment_reads_back_as_the_text_it_was_quoted_from(text):
    create = (
        f"CREATE TABLE `d`.`t` (`a` String CODEC(ZSTD(1)) COMMENT {quote_literal(text)}, "
        "`b` String) ENGINE = MergeTree ORDER BY tuple()"
    )

    assert read_table(create).comments == {"a": text}


def test_a_column_with_no_comment_is_absent():
    create = "CREATE TABLE `d`.`t` (`a` String, `b` String COMMENT 'x') ENGINE = Log"

    assert read_table(create).comments == {"b": "x"}
