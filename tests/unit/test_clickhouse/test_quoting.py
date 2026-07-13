#  Project:      dfe-engine
#  File:         tests/unit/test_clickhouse/test_quoting.py
#  Purpose:      Canonical CH quoting - identifier/literal escaping + masking
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Canonical quoting: identifier + literal escaping + sensitive masking."""

from __future__ import annotations

from dfe_engine.clickhouse.quoting import quote_identifier, quote_literal


def test_quote_identifier_backticks_and_escapes():
    assert quote_identifier("soc-ap") == "`soc-ap`"
    assert quote_identifier("we`ird") == "`we``ird`"


def test_quote_literal_doubles_quotes():
    assert quote_literal("plain") == "'plain'"
    assert quote_literal("O'Brien") == "'O''Brien'"


def test_quote_literal_doubles_backslash_before_quote():
    # F-ROWPOLICY-BACKSLASH: the backslash is doubled (so a crafted value cannot
    # break out of a row-policy predicate), and it is doubled BEFORE the quote.
    assert quote_literal("a\\b") == "'a\\\\b'"
    assert quote_literal("\\'") == "'\\\\'''"
