#  Project:      dfe-engine
#  File:         tests/unit/test_clickhouse/test_quoting.py
#  Purpose:      Canonical CH quoting - identifier/literal escaping + masking
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Canonical quoting: identifier + literal escaping + sensitive masking."""

from __future__ import annotations

import pytest

from dfe_engine.clickhouse.quoting import (
    column_reference,
    quote_identifier,
    quote_literal,
    table_reference,
)


def test_quote_identifier_backticks_and_escapes():
    assert quote_identifier("soc-ap") == "`soc-ap`"
    assert quote_identifier("we`ird") == "`we``ird`"


def test_quote_identifier_doubles_backslash_before_backtick():
    # Unescaped, ClickHouse reads `a\b` as "a" + backspace: a 2-byte name, not 3.
    assert quote_identifier("a\\b") == "`a\\\\b`"
    assert quote_identifier("back\\`tick") == "`back\\\\``tick`"


@pytest.mark.parametrize(
    "name",
    ["acme", "analyst_tier_2", "soc-ap", "a.b", "has space", "o'brien", 'dq"x', "we`ird", "unié"],
)
def test_quote_identifier_is_unchanged_for_a_name_without_a_backslash(name):
    assert quote_identifier(name) == "`" + name.replace("`", "``") + "`"


def test_quote_literal_doubles_quotes():
    assert quote_literal("plain") == "'plain'"
    assert quote_literal("O'Brien") == "'O''Brien'"


def test_quote_literal_doubles_backslash_before_quote():
    # F-ROWPOLICY-BACKSLASH: the backslash is doubled (so a crafted value cannot
    # break out of a row-policy predicate), and it is doubled BEFORE the quote.
    assert quote_literal("a\\b") == "'a\\\\b'"
    assert quote_literal("\\'") == "'\\\\'''"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("severity", "severity"),
        ("_json.user.id", "_json.user.id"),
        ("source.ip", "source.ip"),
        ("field name", "`field name`"),
        ("my-source", "`my-source`"),
        ("field!@#$%", "`field!@#$%`"),
        ("1leading_digit", "`1leading_digit`"),
    ],
)
def test_column_reference_leaves_a_plain_path_bare_and_quotes_the_rest(name, expected):
    assert column_reference(name) == expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("null", "`null`"),
        ("NULL", "`NULL`"),
        ("not", "`not`"),
        ("true", "`true`"),
        ("inf", "`inf`"),
        ("distinct", "`distinct`"),
        ("a.null", "`a`.`null`"),
        # Keywords ClickHouse still resolves as a column when bare stay bare.
        ("select", "select"),
        ("user", "user"),
    ],
)
def test_column_reference_quotes_a_name_clickhouse_reads_as_a_literal(name, expected):
    assert column_reference(name) == expected


def test_column_reference_quotes_a_name_with_a_trailing_newline():
    assert column_reference("severity\n") == "`severity\n`"


@pytest.mark.parametrize("name", ["`severity`", "`a b`", "``"])
def test_column_reference_refuses_an_already_quoted_name(name):
    with pytest.raises(ValueError, match="bare field name"):
        column_reference(name)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("a`b", "`a``b`"),
        ("a\\b", "`a\\\\b`"),
        ("x.a\\b", "`x`.`a\\\\b`"),
        ("x` OR 1=1 OR `y", "`x`` OR 1=1 OR ``y`"),
        ("ts) OR 1=1 --", "`ts) OR 1=1 --`"),
    ],
)
def test_column_reference_quoting_cannot_be_closed_by_the_name(name, expected):
    rendered = column_reference(name)

    assert rendered == expected
    assert rendered.count("`") % 2 == 0


@pytest.mark.parametrize(
    ("reference", "expected"),
    [
        ("events", "`events`"),
        ("db.events", "`db`.`events`"),
        ("`db`.`events`", "`db`.`events`"),
        ("`db`.landing", "`db`.`landing`"),
        ("`dfe`.`my-source`", "`dfe`.`my-source`"),
        (" db.events ", "`db`.`events`"),
    ],
)
def test_table_reference_quotes_every_part(reference, expected):
    assert table_reference(reference) == expected


@pytest.mark.parametrize(
    "reference",
    [
        "",
        "a.b.c",
        "db.events WHERE 1=1",
        "(SELECT name FROM system.users)",
        "db.events; DROP TABLE x",
        "`db`.`ev`ents`",
        "`db\\`.events",
        "db.`events",
        "1db.events",
    ],
)
def test_table_reference_refuses_anything_but_a_table_name(reference):
    with pytest.raises(ValueError, match="not a table reference"):
        table_reference(reference)
