#  Project:      dfe-engine
#  File:         tests/unit/test_clickhouse/test_function_guard_operator_words.py
#  Purpose:      An in() call after an infix operator word is refused, not read as the IN operator
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""ClickHouse reads an operand after MOD, so ``a MOD in(b, t)`` calls in() on table t.

sqlglot tokenises ``MOD`` as a plain name, which the guard would otherwise take
for the end of an operand and so for the IN operator. ``AND``, ``OR``, ``NOT``
and ``XOR`` carry token types of their own and were never read that way; they
are pinned here so a tokenizer change cannot slip them through.
"""

import pytest

from dfe_engine.clickhouse.function_guard import (
    CallNotPermittedError,
    refuse_calls_outside_the_row,
)

# in() straight after an infix operator word, where ClickHouse 26.9.4 parses the call.
AFTER_AN_OPERATOR_WORD = {
    "MOD in brackets": "(bytes MOD in(user, dfe.other)) = 0",
    "lower-case mod": "bytes mod in(user, dfe.other) = 0",
    "MOD on a literal": "10 MOD in(user, dfe.other) = 0",
    "MOD after a call": "length(user) MOD in(user, dfe.other) = 0",
    "MOD before NOT": "bytes MOD NOT in(user, dfe.other) = 0",
    "AND": "a = 1 AND in(user, dfe.other)",
    "OR": "a = 1 OR in(user, dfe.other)",
    "NOT": "NOT in(user, dfe.other)",
    "XOR": "a XOR in(user, dfe.other)",
}

# The IN operator after an operand, which the change must leave alone.
IN_OPERATOR = [
    "x IN (1, 2)",
    "x IN (SELECT ioc FROM threat.iocs)",
    "x NOT IN (1, 2)",
    "x GLOBAL NOT IN (SELECT ioc FROM threat.iocs)",
    "bytes MOD 4 IN (1, 2)",
    "(bytes MOD 4) IN (1, 2)",
    "`mod` IN (1, 2)",
    "modulo IN (1, 2)",
]


@pytest.mark.parametrize("sql", AFTER_AN_OPERATOR_WORD.values(), ids=list(AFTER_AN_OPERATOR_WORD))
def test_an_in_call_after_an_operator_word_is_refused(sql):
    with pytest.raises(CallNotPermittedError) as caught:
        refuse_calls_outside_the_row(sql, subject="A rule's detection condition")

    assert "may not call in(), which can read a table" in str(caught.value)


@pytest.mark.parametrize("sql", IN_OPERATOR)
def test_the_in_operator_after_an_operand_is_left_alone(sql):
    refuse_calls_outside_the_row(sql, subject="filter")


def test_a_column_named_mod_is_told_to_backtick_quote_it():
    with pytest.raises(CallNotPermittedError) as caught:
        refuse_calls_outside_the_row("mod IN (1, 2)", subject="filter")

    assert str(caught.value) == (
        "filter may not call IN(), which can read a table: "
        "IN straight after mod reads as that call, "
        "so a column named mod must be backtick-quoted, as `mod` IN (...)"
    )


def test_the_mod_refusal_names_the_word_it_follows():
    with pytest.raises(CallNotPermittedError) as caught:
        refuse_calls_outside_the_row("(bytes MOD in(user, dfe.other)) = 0", subject="filter")

    assert str(caught.value) == (
        "filter may not call in(), which can read a table: "
        "IN straight after MOD reads as that call, "
        "so a column named MOD must be backtick-quoted, as `MOD` IN (...)"
    )
