"""HdxSanitizer contract: the SQL behind a HyperDX view becomes the base of a rule.

The rule keeps the source table and every user predicate, and loses only the
chrome HyperDX adds to draw the view. Three corpora pin that down:

- golden: SQL rendered by the dfe-hyperdx fork's renderChartConfig, each with
  the exact rule SQL it must reduce to (tests/fixtures/hdx_sanitizer/);
- ugly: hand-written inputs aimed at the places a text rewrite breaks;
- generated: seeded random compositions of user predicates and chrome, checked
  against an oracle built from the same parts, and parsed by sqlglot.
"""

import json
import random
import re
from dataclasses import dataclass
from pathlib import Path

import pytest
import sqlglot
from sqlglot.tokens import TokenType

from dfe_engine.hunts.hdx_sanitizer import HdxSanitizeError, HdxSanitizer
from dfe_engine.hunts.rule_creation_service import RuleCreateRequest, RuleCreationService

FIXTURE = Path(__file__).parents[2] / "fixtures" / "hdx_sanitizer" / "rendered_views.json"
GOLDEN: list[dict] = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]
GOLDEN_KEPT = [case for case in GOLDEN if "clean_sql" in case]
GOLDEN_REFUSED = [case for case in GOLDEN if "refused" in case]

_CHROME_NAMES = re.compile(r"^(?:fromUnixTimestamp\w*|__hdx_\w*)$", re.IGNORECASE)
_CHROME_CLAUSES = ("group", "order", "limit", "offset", "having", "settings", "format", "with")


@pytest.fixture
def sanitizer() -> HdxSanitizer:
    return HdxSanitizer()


def clean(sql: str) -> str:
    return HdxSanitizer().sanitize(sql).clean_sql


def assert_rule_shaped(sql: str) -> None:
    """Assert the SQL parses as one ClickHouse SELECT and carries no view chrome."""
    tree = sqlglot.parse_one(sql, read="clickhouse")
    assert isinstance(tree, sqlglot.exp.Select), sql
    for key in _CHROME_CLAUSES:
        assert not tree.args.get(key), f"{key} survived in {sql}"
    for token in sqlglot.tokenize(sql, read="clickhouse"):
        if token.token_type not in (TokenType.STRING, TokenType.HEREDOC_STRING):
            assert not _CHROME_NAMES.match(token.text.strip('`"')), f"{token.text} in {sql}"


# -- Golden: real renderer output --


class TestGoldenRenderedViews:
    """Every shape the fork's renderer produced reduces to the exact rule SQL."""

    @pytest.mark.parametrize("case", GOLDEN_KEPT, ids=lambda c: c["name"])
    def test_reduces_to_expected_rule_sql(self, case):
        assert clean(case["input"]) == case["clean_sql"]

    @pytest.mark.parametrize("case", GOLDEN_REFUSED, ids=lambda c: c["name"])
    def test_refuses_what_a_rule_cannot_carry(self, case):
        with pytest.raises(HdxSanitizeError, match=re.escape(case["refused"])):
            clean(case["input"])

    @pytest.mark.parametrize(
        "case",
        [c for c in GOLDEN_KEPT if "renderer_defect" not in c],
        ids=lambda c: c["name"],
    )
    def test_output_parses_and_carries_no_chrome(self, case):
        assert_rule_shaped(clean(case["input"]))

    @pytest.mark.parametrize("case", GOLDEN_KEPT, ids=lambda c: c["name"])
    def test_reducing_twice_changes_nothing(self, case):
        once = clean(case["input"])
        assert clean(once) == once

    def test_corpus_covers_both_outcomes(self):
        assert len(GOLDEN_KEPT) >= 30
        assert len(GOLDEN_REFUSED) >= 6

    def test_the_observed_template_leak_is_refused(self):
        leaked = (
            "SELECT _timestamp,_json FROM {{org_id}}.{{source_table_name}} "
            "WHERE ({{timestamp_condition}}) ORDER BY _timestamp DESC"
        )
        with pytest.raises(HdxSanitizeError, match=r"placeholder \{\{org_id\}\}\. "):
            clean(leaked)


# -- Ugly inputs --

WINDOW = (
    "(_timestamp >= fromUnixTimestamp64Milli(1790553600000) "
    "AND _timestamp <= fromUnixTimestamp64Milli(1790557200000))"
)
BASE = "SELECT _timestamp, _json FROM dfe.main"
# A Cyrillic column and value, built from code points so this source stays ASCII.
CYRILLIC_NAME = "".join(map(chr, (0x438, 0x43C, 0x44F)))
CYRILLIC_VALUE = "".join(map(chr, (0x434, 0x430)))
UNICODE_PREDICATE = f"{CYRILLIC_NAME} = '{CYRILLIC_VALUE}'"

UGLY: list[tuple[str, str, str]] = [
    (
        "wrapper_subquery_without_outer_filter",
        f"SELECT * FROM (SELECT _timestamp, _json FROM dfe.main WHERE {WINDOW} AND (a = 1) "
        "ORDER BY _timestamp LIMIT 5) LIMIT 10",
        f"{BASE} WHERE (a = 1)",
    ),
    (
        "user_subquery_keeps_its_own_order_and_limit",
        f"SELECT _timestamp, _json FROM dfe.main WHERE {WINDOW} AND (ip IN (SELECT ip FROM "
        "dfe.blocklist WHERE active = 1 ORDER BY ip LIMIT 100)) ORDER BY _timestamp LIMIT 5",
        f"{BASE} WHERE (ip IN (SELECT ip FROM dfe.blocklist WHERE active = 1 ORDER BY ip "
        "LIMIT 100))",
    ),
    (
        "line_comment_with_keywords_and_braces",
        f"SELECT _timestamp, _json FROM dfe.main -- {{{{org_id}}}} WHERE x LIMIT 5\n"
        f"WHERE {WINDOW} AND (a = 1)",
        f"{BASE} WHERE (a = 1)",
    ),
    (
        "block_comment_inside_a_predicate",
        f"SELECT _timestamp, _json FROM dfe.main WHERE {WINDOW} AND (a /* LIMIT 5 */ = 1)",
        f"{BASE} WHERE (a = 1)",
    ),
    (
        "literal_holding_every_chrome_word",
        f"SELECT _timestamp, _json FROM dfe.main WHERE {WINDOW} AND (msg = 'x WHERE y ORDER BY "
        "z LIMIT 5 SETTINGS a=1 fromUnixTimestamp64Milli(1) __hdx_time_bucket')",
        f"{BASE} WHERE (msg = 'x WHERE y ORDER BY z LIMIT 5 SETTINGS a=1 "
        "fromUnixTimestamp64Milli(1) __hdx_time_bucket')",
    ),
    (
        "literal_holding_template_braces",
        f"SELECT _timestamp, _json FROM dfe.main WHERE {WINDOW} AND (msg LIKE '%{{{{org_id}}}}%')",
        f"{BASE} WHERE (msg LIKE '%{{{{org_id}}}}%')",
    ),
    (
        "escaped_quotes_both_styles",
        f"SELECT _timestamp, _json FROM dfe.main WHERE {WINDOW} AND (note = 'it''s AND done' "
        "AND other = 'a\\'b OR c')",
        f"{BASE} WHERE (note = 'it''s AND done' AND other = 'a\\'b OR c')",
    ),
    (
        "quoted_identifiers_with_braces_and_dots",
        f"SELECT _timestamp, _json FROM dfe.main WHERE {WINDOW} AND (`we{{ird}}.col` = 1 "
        "AND \"x.y\" = 'z')",
        f"{BASE} WHERE (`we{{ird}}.col` = 1 AND \"x.y\" = 'z')",
    ),
    (
        "mixed_case_keywords",
        "select _timestamp from dfe.main wHeRe (_timestamp >= fromUnixTimestamp64Milli(1) and "
        "_timestamp <= fromUnixTimestamp64Milli(2)) and (b = 1) order by _timestamp desc limit 5 "
        "settings max_threads = 1",
        "SELECT _timestamp FROM dfe.main WHERE (b = 1)",
    ),
    (
        "tabs_newlines_and_crlf",
        "SELECT\t_timestamp,\r\n  _json\nFROM\n\tdfe.main\r\nWHERE\n  (\n    _timestamp >= "
        "fromUnixTimestamp64Milli(1)\n    AND _timestamp <= fromUnixTimestamp64Milli(2)\n  )\n"
        "  AND (\n    a = 1\n  )\nLIMIT\n  200 OFFSET 0",
        f"{BASE} WHERE (a = 1)",
    ),
    (
        "json_path_forms",
        f"SELECT _timestamp, _json FROM dfe.main WHERE {WINDOW} AND (_json.a.b = 1) AND "
        "(_json.^a IS NOT NULL) AND (_json.a.:Int64 > 3) AND "
        "(JSONExtractString(_raw, 'k') = 'v') AND (_json.a::String = 'x')",
        f"{BASE} WHERE (_json.a.b = 1) AND (_json.^a IS NOT NULL) AND (_json.a.:Int64 > 3) AND "
        "(JSONExtractString(_raw, 'k') = 'v') AND (_json.a::String = 'x')",
    ),
    (
        "lambda_with_and_inside",
        f"SELECT _timestamp, _json FROM dfe.main WHERE {WINDOW} AND "
        "arrayExists(x -> x = 'a' AND x != 'b', tags)",
        f"{BASE} WHERE arrayExists(x -> x = 'a' AND x != 'b', tags)",
    ),
    (
        "in_lists_and_arrays",
        f"SELECT _timestamp, _json FROM dfe.main WHERE {WINDOW} AND a IN (1, 2, 3) AND "
        "has(['x', 'y'], b)",
        f"{BASE} WHERE a IN (1, 2, 3) AND has(['x', 'y'], b)",
    ),
    (
        "not_over_a_group",
        f"SELECT _timestamp, _json FROM dfe.main WHERE {WINDOW} AND NOT (a = 1 AND b = 2)",
        f"{BASE} WHERE NOT (a = 1 AND b = 2)",
    ),
    (
        "or_group_keeps_its_brackets",
        f"SELECT _timestamp, _json FROM dfe.main WHERE {WINDOW} AND (a = 1 OR b = 2)",
        f"{BASE} WHERE (a = 1 OR b = 2)",
    ),
    (
        "time_bound_mixed_into_a_user_group",
        "SELECT _timestamp, _json FROM dfe.main WHERE ((_timestamp >= "
        "fromUnixTimestamp64Milli(1) AND a = 1) AND _timestamp <= fromUnixTimestamp64Milli(2))",
        f"{BASE} WHERE ((a = 1))",
    ),
    (
        "time_window_last_not_first",
        f"SELECT _timestamp, _json FROM dfe.main WHERE (a = 1) AND {WINDOW}",
        f"{BASE} WHERE (a = 1)",
    ),
    (
        "prewhere_folds_into_where",
        f"SELECT _timestamp, _json FROM dfe.main PREWHERE b = 2 OR c = 3 WHERE {WINDOW} AND a = 1",
        f"{BASE} WHERE (b = 2 OR c = 3) AND a = 1",
    ),
    (
        "prewhere_holding_only_the_window",
        f"SELECT _timestamp, _json FROM dfe.main PREWHERE {WINDOW} WHERE a = 1",
        f"{BASE} WHERE a = 1",
    ),
    (
        "final_is_kept",
        f"SELECT _timestamp, _json FROM dfe.main FINAL WHERE {WINDOW} AND a = 1",
        f"{BASE} FINAL WHERE a = 1",
    ),
    (
        "sample_is_kept",
        f"SELECT _timestamp, _json FROM dfe.main SAMPLE 0.1 WHERE {WINDOW} AND a = 1",
        f"{BASE} SAMPLE 0.1 WHERE a = 1",
    ),
    (
        "format_and_outfile_stripped",
        f"SELECT _timestamp, _json FROM dfe.main WHERE {WINDOW} AND a = 1 "
        "INTO OUTFILE 'x.json' FORMAT JSONEachRow",
        f"{BASE} WHERE a = 1",
    ),
    (
        "with_fill_stripped",
        "SELECT count(), toStartOfInterval(toDateTime(_timestamp), INTERVAL 1 minute) AS "
        f"`__hdx_time_bucket` FROM dfe.main WHERE {WINDOW} AND (a = 1) GROUP BY "
        "`__hdx_time_bucket` ORDER BY `__hdx_time_bucket` WITH FILL STEP 60",
        "SELECT * FROM dfe.main WHERE (a = 1)",
    ),
    (
        "limit_by_then_limit",
        f"SELECT _timestamp, _json FROM dfe.main WHERE {WINDOW} AND a = 1 "
        "LIMIT 5 BY host LIMIT 100",
        f"{BASE} WHERE a = 1",
    ),
    (
        "distinct_projection",
        f"SELECT DISTINCT host FROM dfe.main WHERE {WINDOW} AND a = 1",
        "SELECT DISTINCT host FROM dfe.main WHERE a = 1",
    ),
    (
        "trailing_semicolons",
        f"SELECT _timestamp, _json FROM dfe.main WHERE {WINDOW} AND a = 1;;",
        f"{BASE} WHERE a = 1",
    ),
    (
        "between_inside_an_and_list",
        f"SELECT _timestamp, _json FROM dfe.main WHERE {WINDOW} AND a BETWEEN 1 AND 5 AND b = 2",
        f"{BASE} WHERE a BETWEEN 1 AND 5 AND b = 2",
    ),
    (
        "case_with_and_inside",
        f"SELECT _timestamp, _json FROM dfe.main WHERE {WINDOW} AND "
        "CASE WHEN a = 1 AND b = 2 THEN 1 ELSE 0 END = 1",
        f"{BASE} WHERE CASE WHEN a = 1 AND b = 2 THEN 1 ELSE 0 END = 1",
    ),
    (
        "interval_padded_metric_style_window",
        "SELECT _timestamp, _json FROM dfe.main WHERE (_timestamp >= toStartOfInterval("
        "fromUnixTimestamp64Milli(1), INTERVAL 1 minute) - INTERVAL 1 minute AND _timestamp <= "
        "toStartOfInterval(fromUnixTimestamp64Milli(2), INTERVAL 1 minute) + INTERVAL 1 minute) "
        "AND (a = 1)",
        f"{BASE} WHERE (a = 1)",
    ),
    (
        "between_window",
        "SELECT _timestamp, _json FROM dfe.main WHERE _timestamp BETWEEN "
        "fromUnixTimestamp64Milli(1) AND fromUnixTimestamp64Milli(2) AND a = 1",
        f"{BASE} WHERE a = 1",
    ),
    (
        "mirrored_window",
        "SELECT _timestamp, _json FROM dfe.main WHERE (fromUnixTimestamp64Milli(1) <= _timestamp "
        "AND fromUnixTimestamp64Milli(2) >= _timestamp) AND a = 1",
        f"{BASE} WHERE a = 1",
    ),
    (
        "relative_user_bound_is_user_logic",
        f"SELECT _timestamp, _json FROM dfe.main WHERE {WINDOW} AND "
        "_timestamp > now() - INTERVAL 1 HOUR",
        f"{BASE} WHERE _timestamp > now() - INTERVAL 1 HOUR",
    ),
    (
        "window_function_projection_dropped",
        f"SELECT host, row_number() OVER (PARTITION BY host) AS n FROM dfe.main WHERE {WINDOW} "
        "AND a = 1",
        "SELECT host FROM dfe.main WHERE a = 1",
    ),
    (
        "words_that_contain_clause_names",
        "SELECT limit_count, format_name FROM dfe.main WHERE settings_id = 1 AND "
        "_json.format = 'x' AND _json.limit = 2 AND _json.order.by = 3",
        "SELECT limit_count, format_name FROM dfe.main WHERE settings_id = 1 AND "
        "_json.format = 'x' AND _json.limit = 2 AND _json.order.by = 3",
    ),
    (
        "clause_words_as_column_names",
        "SELECT offset, format, window FROM dfe.kafka WHERE offset > 5 AND format = 'json' "
        "AND window = 1 AND settings = 2 ORDER BY offset LIMIT 10 OFFSET 20",
        "SELECT offset, format, window FROM dfe.kafka WHERE offset > 5 AND format = 'json' "
        "AND window = 1 AND settings = 2",
    ),
    (
        "table_alias_is_kept",
        f"SELECT m._timestamp FROM dfe.main AS m WHERE {WINDOW} AND m.a = 1",
        "SELECT m._timestamp FROM dfe.main AS m WHERE m.a = 1",
    ),
    (
        "heredoc_literal_with_braces",
        f"SELECT _timestamp, _json FROM dfe.main WHERE {WINDOW} AND msg = $$ WHERE {{{{x}}}} $$",
        f"{BASE} WHERE msg = $$ WHERE {{{{x}}}} $$",
    ),
    (
        "whole_statement_in_brackets",
        f"(SELECT _timestamp, _json FROM dfe.main WHERE {WINDOW} AND a = 1)",
        f"{BASE} WHERE a = 1",
    ),
    (
        "select_alias_referenced_by_the_filter",
        "SELECT Timestamp, lower(Body) AS body_lc, SeverityText AS level FROM dfe.otel_logs "
        f"WHERE {WINDOW.replace('_timestamp', 'Timestamp')} AND (level = 'error' AND "
        "body_lc LIKE '%timeout%' AND arrayExists(level -> level = 'x', tags) "
        "AND x IN (SELECT level FROM dfe.levels))",
        "SELECT Timestamp, lower(Body) AS body_lc, SeverityText AS level FROM dfe.otel_logs "
        "WHERE (SeverityText = 'error' AND (lower(Body)) LIKE '%timeout%' AND "
        "arrayExists(level -> level = 'x', tags) AND x IN (SELECT level FROM dfe.levels))",
    ),
    (
        "with_alias_chain",
        "WITH (ServiceName) AS svc, (lower(svc)) AS svc_lc SELECT count() FROM dfe.otel_logs "
        "WHERE svc_lc = 'api'",
        "SELECT * FROM dfe.otel_logs WHERE (lower(ServiceName)) = 'api'",
    ),
    (
        "unicode_identifier_and_literal",
        f"SELECT _timestamp, _json FROM dfe.main WHERE {WINDOW} AND ({UNICODE_PREDICATE})",
        f"{BASE} WHERE ({UNICODE_PREDICATE})",
    ),
]

REFUSALS: list[tuple[str, str, str]] = [
    (
        "double_brace_table",
        "SELECT * FROM {{org_id}}.{{source_table_name}} WHERE a = 1",
        "{{org_id}}",
    ),
    ("single_brace_where", "SELECT * FROM dfe.main WHERE ({timestamp_condition})", "placeholder"),
    (
        "clickhouse_query_parameter",
        "SELECT * FROM dfe.main WHERE _timestamp >= {HYPERDX_PARAM_1:Int64} AND a = 1",
        "placeholder",
    ),
    (
        "from_time_macro",
        "SELECT * FROM dfe.main WHERE _timestamp >= $__fromTime_ms",
        "$__fromTime_ms",
    ),
    ("filters_macro", "SELECT * FROM dfe.main WHERE a = 1 AND $__filters", "$__filters"),
    (
        "window_under_top_level_or",
        "SELECT * FROM dfe.main WHERE _timestamp >= fromUnixTimestamp64Milli(1) AND "
        "_timestamp <= fromUnixTimestamp64Milli(2) AND a = 1 OR b = 2",
        "inside an OR",
    ),
    (
        "window_under_nested_or",
        "SELECT * FROM dfe.main WHERE (_timestamp >= fromUnixTimestamp64Milli(1) OR a = 1)",
        "inside an OR",
    ),
    (
        "window_under_not",
        f"SELECT * FROM dfe.main WHERE NOT {WINDOW} AND a = 1",
        "inside an OR, a NOT",
    ),
    (
        "window_not_between",
        "SELECT * FROM dfe.main WHERE _timestamp NOT BETWEEN fromUnixTimestamp64Milli(1) AND "
        "fromUnixTimestamp64Milli(2)",
        "inside an OR, a NOT",
    ),
    (
        "epoch_equality_is_not_a_window",
        "SELECT * FROM dfe.main WHERE _timestamp = fromUnixTimestamp64Milli(1) AND a = 1",
        "time bound",
    ),
    (
        "bucket_referenced_by_filter",
        "SELECT * FROM dfe.main WHERE `__hdx_time_bucket` > 0",
        "__hdx_time_bucket",
    ),
    (
        "having",
        "SELECT count(), host FROM dfe.main WHERE a = 1 GROUP BY host HAVING count() > 5",
        "HAVING",
    ),
    (
        "qualify",
        "SELECT host FROM dfe.main WHERE a = 1 QUALIFY row_number() OVER () = 1",
        "QUALIFY",
    ),
    ("union_all", "SELECT a FROM dfe.main WHERE x = 1 UNION ALL SELECT a FROM dfe.b", "unions"),
    ("join", "SELECT a FROM dfe.main AS m JOIN dfe.b AS b ON m.id = b.id WHERE x = 1", "joins"),
    ("array_join", "SELECT a FROM dfe.main ARRAY JOIN tags AS t WHERE t = 'x'", "joins"),
    ("comma_join", "SELECT a FROM dfe.main, dfe.b WHERE x = 1", "several tables"),
    ("user_cte", "WITH x AS (SELECT * FROM dfe.main) SELECT * FROM x WHERE a = 1", "CTE x"),
    ("table_function", "SELECT * FROM remote('h', dfe.main) WHERE a = 1", "table function"),
    (
        "derived_table_filter",
        "SELECT * FROM (SELECT a FROM dfe.main WHERE x = 1) WHERE a = 2",
        "derived table",
    ),
    ("not_a_select", "DROP TABLE dfe.main", "not a SELECT"),
    ("insert_select", "INSERT INTO dfe.x SELECT * FROM dfe.main", "not a SELECT"),
    ("no_from", "SELECT 1", "no FROM"),
    ("two_statements", "SELECT a FROM dfe.main; SELECT b FROM dfe.main", "more than one"),
    ("unterminated_string", "SELECT a FROM dfe.main WHERE b = 'x", "unterminated"),
    ("unterminated_comment", "SELECT a FROM dfe.main /* WHERE b = 1", "never closed"),
    ("unbalanced_bracket", "SELECT a FROM dfe.main WHERE (b = 1", "unclosed bracket"),
    ("stray_close", "SELECT a FROM dfe.main WHERE b = 1)", "closing bracket"),
    ("empty_where", "SELECT a FROM dfe.main WHERE", "empty filter"),
    ("empty_select_item", "SELECT a,, b FROM dfe.main WHERE x = 1", "empty column"),
    (
        "nested_past_the_recursion_limit",
        f"SELECT a FROM dfe.main WHERE {'(' * 3000}a = 1{')' * 3000}",
        "nested too deeply",
    ),
    ("clauses_out_of_order", "SELECT a FROM dfe.main ORDER BY a WHERE b = 1", "out of order"),
]


class TestUglyInputs:
    """Inputs aimed at the places a text rewrite of SQL breaks."""

    @pytest.mark.parametrize(("name", "sql", "expected"), UGLY, ids=[u[0] for u in UGLY])
    def test_reduces_to_expected_rule_sql(self, name, sql, expected):
        assert clean(sql) == expected

    @pytest.mark.parametrize(
        ("name", "sql", "expected"),
        [u for u in UGLY if u[0] not in ("json_path_forms", "heredoc_literal_with_braces")],
        ids=[u[0] for u in UGLY if u[0] not in ("json_path_forms", "heredoc_literal_with_braces")],
    )
    def test_output_parses_and_carries_no_chrome(self, name, sql, expected):
        assert_rule_shaped(clean(sql))

    @pytest.mark.parametrize(("name", "sql", "fragment"), REFUSALS, ids=[r[0] for r in REFUSALS])
    def test_refuses_with_a_reason(self, name, sql, fragment):
        with pytest.raises(HdxSanitizeError, match=re.escape(fragment)):
            clean(sql)


# -- Generated queries --


@dataclass(frozen=True)
class Predicate:
    text: str
    bare_or: bool = False


POOL = [
    Predicate("_source = 'winlogbeat'"),
    Predicate("toString(`_json`.`user`.`name`) ILIKE '%bob%'"),
    Predicate("toString(_json.`event`.`code`) IN ('4624', '4625')"),
    Predicate("toString(_json.`user`.`name`) NOT IN ('SYSTEM')"),
    Predicate("msg = 'x WHERE y ORDER BY z LIMIT 5 SETTINGS a = 1'"),
    Predicate("msg LIKE '%{{org_id}}%'"),
    Predicate("note = 'it''s AND done'"),
    Predicate("`we{ird}.col` = 1"),
    Predicate("arrayExists(x -> x = 'a' AND x != 'b', tags)"),
    Predicate("has(['x', 'y'], kind)"),
    Predicate("NOT (a = 1 AND b = 2)"),
    Predicate("a = 1 OR b = 2", bare_or=True),
    Predicate("(c = 3 OR d = 4)"),
    Predicate("bytes BETWEEN 10 AND 5000"),
    Predicate("CASE WHEN level = 'error' AND code > 5 THEN 1 ELSE 0 END = 1"),
    Predicate("JSONExtractString(_raw, 'k') = 'v'"),
    Predicate("ip IN (SELECT ip FROM dfe.blocklist WHERE active = 1 ORDER BY ip LIMIT 100)"),
    Predicate("lower(Body) NOT ILIKE '%timeout%'"),
    Predicate("Duration > 1000000"),
    Predicate("_timestamp > now() - INTERVAL 1 HOUR"),
    Predicate("notEmpty(toString(`_json`.`user`.`name`)) = 1"),
    Predicate("indexHint(mapContains(`LogAttributes`, 'k'))"),
    Predicate("`LogAttributes`['http.status_code'] = '500'"),
    Predicate("positionCaseInsensitive(Body, 'fail') > 0"),
    Predicate("match(Body, '^ERROR.*(timeout|refused)$')"),
    Predicate("x IS NOT NULL"),
    Predicate("multiIf(a = 1, 'x', b = 2, 'y', 'z') = 'x'"),
    Predicate("toUInt32OrZero(code) >= 400"),
]

WINDOWS = [
    "({c} >= fromUnixTimestamp64Milli({s}) AND {c} <= fromUnixTimestamp64Milli({e}))",
    "({c} > fromUnixTimestamp64Milli({s}) AND {c} < fromUnixTimestamp64Milli({e}))",
    "({c} >= toDate(fromUnixTimestamp64Milli({s})) AND {c} <= toDate(fromUnixTimestamp64Milli({e})))",
    "(toStartOfDay({c}) >= toStartOfDay(fromUnixTimestamp64Milli({s})) AND "
    "toStartOfDay({c}) <= toStartOfDay(fromUnixTimestamp64Milli({e})))",
    "({c} >= toStartOfInterval(fromUnixTimestamp64Milli({s}), INTERVAL 1 minute) - INTERVAL 1 "
    "minute AND {c} <= toStartOfInterval(fromUnixTimestamp64Milli({e}), INTERVAL 1 minute) + "
    "INTERVAL 1 minute)",
    "({c} BETWEEN fromUnixTimestamp64Milli({s}) AND fromUnixTimestamp64Milli({e}))",
    "(d >= toDate(fromUnixTimestamp64Milli({s})) AND d <= toDate(fromUnixTimestamp64Milli({e})))"
    "AND({c} >= fromUnixTimestamp64Milli({s}) AND {c} <= fromUnixTimestamp64Milli({e}))",
]

COMMENTS = [
    "/* WHERE {{org_id}} LIMIT 5 */",
    "-- ORDER BY x SETTINGS a = 1 fromUnixTimestamp64Milli(1)\n",
    "/* __hdx_time_bucket */",
]

BUCKET = "toStartOfInterval(toDateTime({c}), INTERVAL 1 minute) AS `__hdx_time_bucket`"


def _keyword(rng: random.Random, word: str) -> str:
    choice = rng.randrange(3)
    return word if choice == 0 else word.lower() if choice == 1 else word.title()


def _spread(rng: random.Random, text: str) -> str:
    """Widen the spaces outside quotes into random whitespace runs."""
    out: list[str] = []
    quote = ""
    for ch in text:
        if quote:
            out.append(ch)
            if ch == quote:
                quote = ""
        elif ch in "'`\"":
            quote = ch
            out.append(ch)
        elif ch == " ":
            out.append(rng.choice([" ", "  ", "\n  ", "\t", "\r\n"]))
        else:
            out.append(ch)
    return "".join(out)


def _gap(rng: random.Random) -> str:
    return f" {rng.choice(COMMENTS)} " if rng.random() < 0.3 else rng.choice([" ", "\n", "\n  "])


def generate(seed: int, hazard: tuple[str, str] | None = None) -> tuple[str, str, list[Predicate]]:
    """Compose one HyperDX-shaped query and the rule SQL it must reduce to.

    ``hazard`` is ``(text, place)``: text spliced in as the table, as one more
    WHERE conjunct, or as a clause after the filter. The oracle ignores it.
    """
    rng = random.Random(seed)
    table, column = rng.choice([("dfe.main", "_timestamp"), ("dfe.otel_logs", "Timestamp")])
    chosen = rng.sample(POOL, rng.randint(0, 4))

    conjuncts: list[str] = []
    expected_where: list[str] = []
    for pred in chosen:
        wrapped = pred.bare_or or rng.random() < 0.6
        text = f"({pred.text})" if wrapped else pred.text
        conjuncts.append(_spread(rng, text))
        expected_where.append(text)
    if rng.random() < 0.85:
        start = rng.randint(10**12, 2 * 10**12)
        window = rng.choice(WINDOWS).format(c=column, s=start, e=start + 3_600_000)
        conjuncts.insert(rng.randint(0, len(conjuncts)), window)
    if hazard and hazard[1] == "where":
        conjuncts.insert(rng.randint(0, len(conjuncts)), hazard[0])
    source = hazard[0] if hazard and hazard[1] == "table" else table

    chart = rng.random() < 0.4
    group_key = chart and rng.random() < 0.5
    if chart:
        items = ["count()"] + (["host"] if group_key else []) + [BUCKET.format(c=column)]
        expected_projection = "host" if group_key else "*"
    else:
        items = [column, "_json"] if table == "dfe.main" else [column, "Body"]
        expected_projection = ", ".join(items)

    sql = [_keyword(rng, "SELECT"), " ", ",".join(items), _gap(rng), _keyword(rng, "FROM")]
    sql += [" ", source]
    if conjuncts:
        joiner = f" {_keyword(rng, 'AND')} "
        sql += [_gap(rng), _keyword(rng, "WHERE"), " ", joiner.join(conjuncts)]
    if chart or (hazard and hazard[0].startswith("HAVING")):
        keys = (["host"] if group_key else []) + [BUCKET.format(c=column)]
        sql += [_gap(rng), _keyword(rng, "GROUP"), " BY ", ",".join(keys)]
    if hazard and hazard[1] == "tail":
        sql += [" ", hazard[0]]
    if chart:
        sql += [" ", _keyword(rng, "ORDER"), " BY ", BUCKET.format(c=column)]
    elif rng.random() < 0.7:
        sql += [_gap(rng), _keyword(rng, "ORDER"), " BY ", f"{column} DESC"]
    if rng.random() < 0.6:
        sql += [_gap(rng), _keyword(rng, "LIMIT"), f" {rng.randint(1, 500)}"]
        if rng.random() < 0.5:
            sql += [" ", _keyword(rng, "OFFSET"), f" {rng.randint(0, 5000)}"]
    if rng.random() < 0.7:
        sql += [_gap(rng), _keyword(rng, "SETTINGS"), " optimize_read_in_order = 0, x = 'y'"]
    if rng.random() < 0.2:
        sql += [" ", _keyword(rng, "FORMAT"), " JSONEachRow"]

    expected = f"SELECT {expected_projection} FROM {table}"
    if expected_where:
        expected = f"{expected} WHERE {' AND '.join(expected_where)}"
    return "".join(sql), expected, chosen


REFUSAL_INJECTIONS = [
    ("{{org_id}}.{{source_table_name}}", "table"),
    ("({timestamp_condition})", "where"),
    ("_timestamp >= {HYPERDX_PARAM_1:Int64}", "where"),
    ("_timestamp >= $__fromTime_ms", "where"),
    ("(_timestamp >= fromUnixTimestamp64Milli(1) OR a = 1)", "where"),
    ("NOT (_timestamp >= fromUnixTimestamp64Milli(1))", "where"),
    ("HAVING count() > 1", "tail"),
    ("UNION ALL SELECT a FROM dfe.b", "tail"),
]


class TestGeneratedQueries:
    """Seeded compositions of user predicates and chrome, checked against an oracle."""

    @pytest.mark.parametrize("seed", range(400))
    def test_reduces_to_the_oracle(self, seed):
        sql, expected, _ = generate(seed)
        assert clean(sql) == expected, sql

    @pytest.mark.parametrize("seed", range(0, 400, 4))
    def test_output_parses_carries_no_chrome_and_is_stable(self, seed):
        sql, _, chosen = generate(seed)
        once = clean(sql)
        assert_rule_shaped(once)
        assert clean(once) == once
        for pred in chosen:
            assert pred.text in once

    @pytest.mark.parametrize("seed", range(160))
    def test_injected_hazard_is_refused(self, seed):
        sql, _, _ = generate(seed, REFUSAL_INJECTIONS[seed % len(REFUSAL_INJECTIONS)])
        with pytest.raises(HdxSanitizeError):
            clean(sql)


# -- The record of what was removed --


class TestResultRecord:
    """The result says what was removed, so the API can show it."""

    def test_each_window_comparison_is_recorded(self, sanitizer):
        result = sanitizer.sanitize(f"SELECT * FROM dfe.main WHERE {WINDOW} AND a = 1")
        assert result.stripped_time_bounds == [
            "_timestamp >= fromUnixTimestamp64Milli(1790553600000)",
            "_timestamp <= fromUnixTimestamp64Milli(1790557200000)",
        ]

    def test_limit_offset_and_settings_are_recorded_verbatim(self, sanitizer):
        result = sanitizer.sanitize(
            "SELECT a FROM dfe.main WHERE b = 1 LIMIT 10 OFFSET 20 "
            "SETTINGS optimize_read_in_order = 0, cast_keep_nullable = 1"
        )
        assert result.stripped_limit == "LIMIT 10 OFFSET 20"
        assert result.stripped_settings == (
            "SETTINGS optimize_read_in_order = 0, cast_keep_nullable = 1"
        )

    def test_every_limit_is_recorded_in_order(self, sanitizer):
        result = sanitizer.sanitize(
            "SELECT * FROM (SELECT a FROM dfe.main WHERE b = 1 LIMIT 5 BY a LIMIT 100) LIMIT 10"
        )
        assert result.clean_sql == "SELECT a FROM dfe.main WHERE b = 1"
        assert result.stripped_limit == "LIMIT 10 LIMIT 5 BY a LIMIT 100"

    def test_time_bucket_is_recorded(self, sanitizer):
        sql = next(c["input"] for c in GOLDEN if c["name"] == "histogram_main")
        result = sanitizer.sanitize(sql)
        assert result.had_time_bucket is True
        assert result.stripped_time_bucket_select == [
            "toStartOfInterval(toDateTime(_timestamp), INTERVAL 1 minute) AS `__hdx_time_bucket`"
        ]
        assert len(result.stripped_time_bucket_refs) == 2
        assert "SELECT count()" in result.stripped_clauses

    def test_series_cap_is_recorded(self, sanitizer):
        sql = next(
            c["input"] for c in GOLDEN if c["name"] == "timechart_count_by_service_series_limit"
        )
        result = sanitizer.sanitize(sql)
        assert any(s.startswith("WITH `__hdx_series_limit` AS") for s in result.stripped_clauses)
        assert any("IN (SELECT `group`" in s for s in result.stripped_clauses)

    def test_clean_input_records_nothing(self, sanitizer):
        sql = "SELECT 1 FROM default.logs WHERE severity = 'high'"
        result = sanitizer.sanitize(sql)
        assert result.clean_sql == sql
        assert result.stripped_time_bounds == []
        assert result.stripped_clauses == []
        assert result.stripped_settings is None
        assert result.stripped_limit is None
        assert result.had_time_bucket is False
        assert result.warnings == []

    def test_no_remaining_filter_is_a_warning(self, sanitizer):
        result = sanitizer.sanitize(f"SELECT * FROM dfe.main WHERE {WINDOW}")
        assert result.clean_sql == "SELECT * FROM dfe.main"
        assert result.warnings == [
            "No filter remains once the view's time window is removed, so a rule built "
            "from this SQL matches every row of dfe.main."
        ]

    def test_final_and_sample_are_warned_about(self, sanitizer):
        result = sanitizer.sanitize("SELECT * FROM dfe.main FINAL SAMPLE 0.5 WHERE a = 1")
        assert [w.split(",")[0] for w in result.warnings] == [
            "The table reference keeps FINAL",
            "The table reference keeps SAMPLE",
        ]

    @pytest.mark.parametrize("sql", ["", "   ", "\n\t"])
    def test_empty_input_gives_empty_sql(self, sanitizer, sql):
        result = sanitizer.sanitize(sql)
        assert result.clean_sql == ""
        assert result.warnings == []


# -- The call site --


class TestRuleCreationCallSite:
    """RuleCreationService turns a refusal into a SQL error instead of a crash."""

    def test_refusal_becomes_a_sql_error(self):
        leaked = next(c["input"] for c in GOLDEN if c["name"] == "export_sql_placeholder_route")
        result = RuleCreationService().create_rule(
            RuleCreateRequest(name="Leak", source_type="hyperdx", user_sql=leaked),
            rule_id="leak",
        )
        assert len(result.sql_errors) == 1
        assert "unrendered template placeholder" in result.sql_errors[0].message
        assert result.sanitize_summary == {}

    def test_rendered_search_becomes_the_rule_filter(self):
        sql = next(c["input"] for c in GOLDEN if c["name"] == "search_main_sql_where_and_facets")
        result = RuleCreationService().create_rule(
            RuleCreateRequest(name="Facets", source_type="hyperdx", user_sql=sql),
            rule_id="facets",
        )
        assert result.sql_errors == []
        assert result.rule.source_db == "dfe"
        assert result.rule.source_table == "main"
        assert result.rule.where_clause == (
            "(_source = 'winlogbeat') AND ((toString(_json.`event`.`code`) IN ('4624', '4625')) "
            "AND (toString(_json.`user`.`name`) NOT IN ('SYSTEM')))"
        )
        assert "ORDER BY _timestamp DESC" in result.sanitize_summary["stripped_clauses"]
