#  Project:      dfe-engine
#  File:         tests/unit/test_rule_authoring.py
#  Purpose:      Rule-authoring helpers - HyperDX strip + query scaffold
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""WF1 (HyperDX -> rule) + WF2 (query scaffold)."""

import json
from pathlib import Path

import pytest
import sqlglot

from dfe_engine.hunts.hdx_sanitizer import HdxSanitizer
from dfe_engine.rule_authoring import build_query_scaffold, strip_hyperdx

GOLDEN = {
    case["name"]: case
    for case in json.loads(
        (
            Path(__file__).parents[1] / "fixtures" / "hdx_sanitizer" / "rendered_views.json"
        ).read_text(encoding="utf-8")
    )["cases"]
}


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


def test_real_hyperdx_search_keeps_brackets_balanced():
    out = strip_hyperdx(GOLDEN["search_main_sql_where_and_facets"]["input"])
    assert out["query"] == (
        "SELECT _timestamp,_json FROM dfe.main WHERE {window} AND (_source = 'winlogbeat') AND "
        "((toString(_json.`event`.`code`) IN ('4624', '4625')) AND "
        "(toString(_json.`user`.`name`) NOT IN ('SYSTEM'))) ORDER BY _timestamp DESC LIMIT 200 "
        "OFFSET 0 SETTINGS optimize_read_in_order = 0, cast_keep_nullable = 1"
    )
    assert (out["replaced"], out["needs_window"], out["warnings"]) == (True, False, [])


def test_real_hyperdx_chart_keeps_the_chart():
    out = strip_hyperdx(GOLDEN["histogram_main"]["input"])
    assert out["query"] == (
        "SELECT count(),toStartOfInterval(toDateTime(_timestamp), INTERVAL 1 minute) AS "
        "`__hdx_time_bucket` FROM dfe.main WHERE {window} AND "
        "((toString(`_json`.`user`.`name`) ILIKE '%bob%')) GROUP BY "
        "toStartOfInterval(toDateTime(_timestamp), INTERVAL 1 minute) AS `__hdx_time_bucket` "
        "ORDER BY toStartOfInterval(toDateTime(_timestamp), INTERVAL 1 minute) AS "
        "`__hdx_time_bucket` SETTINGS optimize_read_in_order = 0, cast_keep_nullable = 1"
    )


def test_epoch_window_on_a_column_not_in_time_fields():
    out = strip_hyperdx(GOLDEN["search_otel_sql_alias_ref"]["input"])
    assert out["replaced"] is True
    assert "WHERE {window} AND (level = 'error' AND Body ILIKE '%timeout%')" in out["query"]


@pytest.mark.parametrize(
    "case",
    [c for c in GOLDEN.values() if "clean_sql" in c],
    ids=lambda c: c["name"],
)
def test_removes_the_same_window_as_the_sanitizer(case):
    """One parser: the authoring strip takes out exactly the sanitizer's comparisons."""
    stripped = HdxSanitizer().sanitize(case["input"]).stripped_time_bounds
    out = strip_hyperdx(case["input"], time_fields=["no_such_column"])
    assert out["replaced"] is bool(stripped)
    before = case["input"].count("fromUnixTimestamp64Milli")
    assert out["query"].count("fromUnixTimestamp64Milli") == before - len(stripped)


def _parses(sql: str) -> bool:
    try:
        sqlglot.parse_one(sql, read="clickhouse")
    except sqlglot.errors.ParseError:
        return False
    return True


# sqlglot cannot read HyperDX's GROUP BY <expr> AS <alias>, window or not.
PARSEABLE = [c for c in GOLDEN.values() if "clean_sql" in c and _parses(c["input"])]


@pytest.mark.parametrize("case", PARSEABLE, ids=lambda c: c["name"])
def test_output_parses_wherever_the_input_did(case):
    # The runner substitutes a predicate for {window}; 1 = 1 stands in for it.
    out = strip_hyperdx(case["input"])
    assert _parses(out["query"].replace("{window}", "1 = 1"))


def test_most_rendered_views_are_parse_checked():
    assert len(PARSEABLE) >= 20


def test_bound_text_inside_a_literal_is_data():
    q = "SELECT * FROM t WHERE msg = '_timestamp >= 1 AND _timestamp < 2' AND x = 1"
    out = strip_hyperdx(q)
    assert (out["query"], out["replaced"], out["needs_window"]) == (q, False, True)


def test_bound_under_or_stays_and_is_reported():
    q = "SELECT * FROM t WHERE _timestamp >= 1 AND _timestamp < 9 OR x = 1"
    out = strip_hyperdx(q)
    assert (out["query"], out["replaced"]) == (q, False)
    assert out["warnings"] == [
        "A time bound sits under OR, NOT or a function call and stays in the query: "
        "_timestamp >= 1 AND _timestamp < 9 OR x = 1"
    ]


def test_single_bound_is_replaced():
    out = strip_hyperdx("SELECT * FROM t WHERE _timestamp > now() - INTERVAL 1 HOUR AND x = 1")
    assert out["query"] == "SELECT * FROM t WHERE {window} AND x = 1"


def test_prewhere_bound_moves_the_filter_into_where():
    out = strip_hyperdx("SELECT * FROM t PREWHERE _timestamp >= 1 AND _timestamp < 9 WHERE x = 1")
    assert out["query"] == "SELECT * FROM t WHERE {window} AND x = 1"


def test_or_filter_is_bracketed_behind_the_window():
    out = strip_hyperdx("SELECT * FROM t WHERE (_timestamp >= 1) AND (a = 1 OR b = 2)")
    assert out["query"] == "SELECT * FROM t WHERE {window} AND (a = 1 OR b = 2)"


def test_query_already_holding_window_needs_none():
    out = strip_hyperdx("SELECT * FROM t WHERE {window} AND x = 1")
    assert (out["replaced"], out["needs_window"]) == (False, False)


@pytest.mark.parametrize(
    "query",
    ["SELECT * FROM t WHERE msg = 'unterminated", "SELECT * FROM t WHERE (x = 1", "DROP TABLE t"],
)
def test_unreadable_query_comes_back_unchanged_with_a_warning(query):
    out = strip_hyperdx(query)
    assert (out["query"], out["replaced"], out["needs_window"]) == (query, False, True)
    assert out["warnings"][0].startswith("Could not read the query: ")


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


@pytest.mark.parametrize(
    ("source", "predicate"),
    [
        ("okta", "_source = 'okta'"),
        ("o'brien", "_source = 'o''brien'"),
        ("back\\slash", "_source = 'back\\\\slash'"),
        ("x' OR 1=1 --", "_source = 'x'' OR 1=1 --'"),
    ],
)
def test_the_scaffold_quotes_the_source_name(source: str, predicate: str):
    """A name holding a quote must not end the literal, or the scaffold is unusable SQL."""
    sql = build_query_scaffold(["a"], "`dfe`.`events`", source=source)

    assert predicate in sql
