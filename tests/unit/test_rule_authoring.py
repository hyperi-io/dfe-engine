#  Project:      dfe-engine
#  File:         tests/unit/test_rule_authoring.py
#  Purpose:      Rule-authoring helpers - HyperDX strip + query scaffold
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""WF1 (HyperDX -> rule) + WF2 (query scaffold)."""

from __future__ import annotations

from dfe_engine.rule_authoring import build_query_scaffold, strip_hyperdx


def test_strip_paired_time_range_to_window():
    q = (
        "SELECT * FROM logs WHERE _timestamp >= toDateTime('2026-01-01') "
        "AND _timestamp < toDateTime('2026-01-02') AND level = 'error'"
    )
    out = strip_hyperdx(q)
    assert out["replaced"] is True
    assert "{window}" in out["query"]
    assert "toDateTime" not in out["query"]  # the time bounds are gone
    assert "level = 'error'" in out["query"]  # the rest is preserved


def test_strip_between_to_window():
    q = "SELECT a FROM t WHERE timestamp BETWEEN 1000 AND 2000 GROUP BY a"
    out = strip_hyperdx(q)
    assert out["replaced"] is True
    assert "{window}" in out["query"]
    assert "BETWEEN" not in out["query"]
    assert "GROUP BY a" in out["query"]


def test_no_time_bound_flags_needs_window():
    q = "SELECT count() FROM t WHERE level = 'error'"
    out = strip_hyperdx(q)
    assert out["replaced"] is False
    assert out["needs_window"] is True
    assert out["query"] == "SELECT count() FROM t WHERE level = 'error'"


def test_custom_time_field():
    q = "SELECT * FROM t WHERE evt_time >= 1 AND evt_time < 9"
    out = strip_hyperdx(q, time_fields=["evt_time"])
    assert out["replaced"] is True
    assert "{window}" in out["query"]


def test_build_query_scaffold():
    sql = build_query_scaffold(["a", "b", "c"], "`dfe`.`events`", source="okta", limit=50)
    assert "SELECT a, b, c" in sql
    assert "FROM `dfe`.`events`" in sql
    assert "_source = 'okta'" in sql
    assert "{window}" in sql
    assert "LIMIT 50" in sql


def test_build_scaffold_no_columns_selects_star():
    sql = build_query_scaffold([], "`dfe`.`events`")
    assert "SELECT *" in sql
    assert "{window}" in sql
