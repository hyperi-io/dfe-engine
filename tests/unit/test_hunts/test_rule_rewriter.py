"""Tests for RuleRewriter — SQL parsing and time bound stripping."""

import random
import re
import time

import pytest

from dfe_engine.hunts.rule_rewriter import (
    TIME_PLACEHOLDER,
    ParsedRule,
    RuleRewriter,
    strip_time_placeholder,
)


@pytest.fixture
def rewriter():
    return RuleRewriter()


class TestSelectStarDetection:
    """Test SELECT * detection."""

    def test_detects_select_star(self, rewriter):
        """Should flag SELECT * in the SQL."""
        result = rewriter.parse_user_sql("SELECT * FROM acme.windows_audit WHERE severity = 'high'")
        assert result.had_select_star is True
        assert any("SELECT *" in w for w in result.warnings)

    def test_no_select_star(self, rewriter):
        """Should not flag when columns are named."""
        result = rewriter.parse_user_sql(
            "SELECT severity, source_ip FROM acme.windows_audit WHERE severity = 'high'"
        )
        assert result.had_select_star is False

    def test_select_star_case_insensitive(self, rewriter):
        """Should detect case variations."""
        result = rewriter.parse_user_sql("select * from acme.windows_audit WHERE x = 1")
        assert result.had_select_star is True


class TestSourceTableExtraction:
    """Test FROM clause parsing."""

    def test_extracts_qualified_table(self, rewriter):
        """Should extract db.table from qualified name."""
        result = rewriter.parse_user_sql("SELECT * FROM acme.windows_audit WHERE x = 1")
        assert result.source_db == "acme"
        assert result.source_table == "windows_audit"

    def test_extracts_unqualified_table(self, rewriter):
        """Should extract table name without db prefix."""
        result = rewriter.parse_user_sql("SELECT * FROM windows_audit WHERE x = 1")
        assert result.source_db is None
        assert result.source_table == "windows_audit"

    def test_no_from_clause(self, rewriter):
        """Should warn when no FROM clause found."""
        result = rewriter.parse_user_sql("SELECT 1")
        assert result.source_table is None
        assert any("source table" in w.lower() for w in result.warnings)


class TestTimeBoundStripping:
    """Test time-bound condition removal."""

    def test_strips_timestamp_gte(self, rewriter):
        """Should strip timestamp >= 'datetime' conditions."""
        result = rewriter.parse_user_sql(
            "SELECT * FROM t WHERE timestamp >= '2026-01-01 00:00:00' AND severity = 'high'"
        )
        assert "timestamp >=" not in result.where_clause
        assert "severity = 'high'" in result.where_clause
        assert len(result.stripped_time_bounds) >= 1

    def test_strips_timestamp_lt(self, rewriter):
        """Should strip timestamp < 'datetime' conditions."""
        result = rewriter.parse_user_sql(
            "SELECT * FROM t WHERE severity = 'high' AND _timestamp_load < '2026-01-01 12:00:00'"
        )
        assert "_timestamp_load <" not in result.where_clause
        assert "severity = 'high'" in result.where_clause

    def test_strips_both_bounds(self, rewriter):
        """Should strip both >= and < time bounds."""
        result = rewriter.parse_user_sql(
            "SELECT * FROM t WHERE "
            "_timestamp >= '2026-01-01' AND _timestamp < '2026-01-02' "
            "AND process_executable = 'cmd.exe'"
        )
        assert "_timestamp >=" not in result.where_clause
        assert "_timestamp <" not in result.where_clause
        assert "process_executable = 'cmd.exe'" in result.where_clause
        assert len(result.stripped_time_bounds) == 2

    def test_strips_now_interval(self, rewriter):
        """Should strip now() - INTERVAL patterns."""
        result = rewriter.parse_user_sql(
            "SELECT * FROM t WHERE _timestamp_load >= now() - INTERVAL 1 HOUR "
            "AND severity = 'critical'"
        )
        assert "now()" not in result.where_clause
        assert "severity = 'critical'" in result.where_clause

    def test_strips_between(self, rewriter):
        """Should strip BETWEEN time bounds."""
        result = rewriter.parse_user_sql(
            "SELECT * FROM t WHERE timestamp BETWEEN '2026-01-01' AND '2026-01-02' AND rule_id = 5"
        )
        assert "BETWEEN" not in result.where_clause
        assert "rule_id = 5" in result.where_clause

    def test_preserves_non_timestamp_conditions(self, rewriter):
        """Non-timestamp conditions should be preserved."""
        result = rewriter.parse_user_sql(
            "SELECT * FROM t WHERE "
            "process_executable IN ('certutil.exe') "
            "AND match(process_commandline, '(?i:EDITF)') "
            "AND _timestamp >= '2026-01-01'"
        )
        assert "process_executable IN ('certutil.exe')" in result.where_clause
        assert "match(process_commandline" in result.where_clause
        assert "_timestamp >=" not in result.where_clause

    def test_no_where_clause(self, rewriter):
        """Should handle SQL without WHERE clause."""
        result = rewriter.parse_user_sql("SELECT * FROM acme.windows_audit")
        assert result.where_clause == ""
        assert any("WHERE" in w.upper() for w in result.warnings)

    def test_only_time_bounds(self, rewriter):
        """WHERE with only time bounds should result in empty clause."""
        result = rewriter.parse_user_sql(
            "SELECT * FROM t WHERE timestamp >= '2026-01-01' AND timestamp < '2026-01-02'"
        )
        assert result.where_clause == ""

    def test_strips_jinja2_timestamp_placeholders(self, rewriter):
        """Should strip Jinja2 timestamp placeholders."""
        result = rewriter.parse_user_sql(
            "SELECT * FROM t WHERE timestamp >= {{from_timestamp}} "
            "AND timestamp < {{to_timestamp}} AND severity = 'high'"
        )
        assert "{{from_timestamp}}" not in result.where_clause
        assert "{{to_timestamp}}" not in result.where_clause
        assert "severity = 'high'" in result.where_clause


class TestBooleanCleanup:
    """Test cleanup of residual boolean operators."""

    def test_removes_leading_and(self, rewriter):
        """Should remove leading AND after stripping."""
        result = rewriter.parse_user_sql(
            "SELECT * FROM t WHERE _timestamp >= '2026-01-01' AND x = 1"
        )
        assert not result.where_clause.startswith("AND")

    def test_removes_trailing_and(self, rewriter):
        """Should remove trailing AND after stripping."""
        result = rewriter.parse_user_sql(
            "SELECT * FROM t WHERE x = 1 AND _timestamp < '2026-01-02'"
        )
        assert not result.where_clause.endswith("AND")

    def test_removes_double_and(self, rewriter):
        """Should clean up AND AND from middle removal."""
        result = rewriter.parse_user_sql(
            "SELECT * FROM t WHERE x = 1 AND _timestamp >= '2026-01-01' AND y = 2"
        )
        assert "AND AND" not in result.where_clause.upper()
        assert "x = 1" in result.where_clause
        assert "y = 2" in result.where_clause


class TestCustomTimestampColumns:
    """Test custom timestamp column detection."""

    def test_additional_timestamp_columns(self):
        """Should strip custom timestamp columns when configured."""
        rewriter = RuleRewriter(timestamp_columns=frozenset({"event_time", "created_at"}))
        result = rewriter.parse_user_sql(
            "SELECT * FROM t WHERE event_time >= '2026-01-01' AND x = 1"
        )
        assert "event_time" not in result.where_clause
        assert "x = 1" in result.where_clause


class TestParsedRuleDefaults:
    """Test ParsedRule dataclass defaults."""

    def test_defaults(self):
        """ParsedRule should have sensible defaults."""
        result = ParsedRule()
        assert result.source_db is None
        assert result.source_table is None
        assert result.where_clause == ""
        assert result.had_select_star is False
        assert result.stripped_time_bounds == []
        assert result.original_sql == ""
        assert result.warnings == []

    def test_preserves_original_sql(self, rewriter):
        """Should store the original SQL."""
        sql = "SELECT * FROM t WHERE x = 1"
        result = rewriter.parse_user_sql(sql)
        assert result.original_sql == sql


# Each case failed on the regex rewriter: a clause word in a literal or comment
# ended the WHERE, a quoted table was not found, or a trailing clause leaked in.
STATEMENT_CASES = [
    (
        "literal_holding_order_by",
        "SELECT _timestamp, _json FROM dfe.main WHERE (msg = 'failed ORDER BY clause') AND (a = 1)",
        ("dfe", "main", "(msg = 'failed ORDER BY clause') AND (a = 1)"),
    ),
    (
        "literal_holding_where_limit",
        "SELECT _timestamp FROM dfe.main WHERE (toString(`_json`.`message`) = 'WHERE LIMIT x')",
        ("dfe", "main", "(toString(`_json`.`message`) = 'WHERE LIMIT x')"),
    ),
    (
        "literal_holding_group_by_having_semicolon",
        "SELECT * FROM dfe.main WHERE msg = 'GROUP BY a HAVING b; c' AND x = 1",
        ("dfe", "main", "msg = 'GROUP BY a HAVING b; c' AND x = 1"),
    ),
    (
        "backtick_table",
        "SELECT _timestamp FROM `dfe`.`main` WHERE a = 1",
        ("dfe", "main", "a = 1"),
    ),
    (
        "backtick_table_with_dash",
        "SELECT * FROM `dfe`.`win-events` WHERE a = 1",
        ("dfe", "win-events", "a = 1"),
    ),
    (
        "settings_after_where",
        "SELECT * FROM dfe.main WHERE a = 1 SETTINGS max_threads = 2",
        ("dfe", "main", "a = 1"),
    ),
    (
        "format_after_where",
        "SELECT * FROM dfe.main WHERE a = 1 FORMAT JSONEachRow",
        ("dfe", "main", "a = 1"),
    ),
    (
        "prewhere_is_a_filter",
        "SELECT * FROM dfe.main PREWHERE _source = 'x' WHERE a = 1",
        ("dfe", "main", "_source = 'x' AND a = 1"),
    ),
    (
        "scalar_subquery_before_from",
        "SELECT (SELECT max(v) FROM dfe.other WHERE k = 1) AS m, a FROM dfe.main WHERE b = 2",
        ("dfe", "main", "b = 2"),
    ),
    (
        "time_bound_under_or_is_left_in_place",
        "SELECT * FROM dfe.main WHERE _timestamp > '2026-01-01' OR severity = 'high'",
        ("dfe", "main", "(_timestamp > '2026-01-01' OR severity = 'high')"),
    ),
    (
        "time_bound_inside_a_literal",
        "SELECT * FROM dfe.main WHERE msg = 'x AND timestamp > ''2026'' AND y' AND a = 1",
        ("dfe", "main", "msg = 'x AND timestamp > ''2026'' AND y' AND a = 1"),
    ),
    (
        "select_alias_in_filter",
        "SELECT SeverityText AS level FROM dfe.otel_logs WHERE level = 'error'",
        ("dfe", "otel_logs", "SeverityText = 'error'"),
    ),
    (
        "block_comment_holding_clause_words",
        "SELECT * FROM dfe.main WHERE a = 1 /* ORDER BY x */ AND b = 2",
        ("dfe", "main", "a = 1 AND b = 2"),
    ),
    (
        "epoch_window_on_any_column",
        "SELECT * FROM dfe.otel_logs WHERE (Timestamp >= fromUnixTimestamp64Milli(1) "
        "AND Timestamp <= fromUnixTimestamp64Milli(2)) AND (a = 1)",
        ("dfe", "otel_logs", "(a = 1)"),
    ),
]


class TestReadsTheStatement:
    """The rewriter reads SQL structure, so literals and comments are data."""

    @pytest.mark.parametrize(
        ("name", "sql", "expected"), STATEMENT_CASES, ids=[c[0] for c in STATEMENT_CASES]
    )
    def test_table_and_filter(self, rewriter, name, sql, expected):
        result = rewriter.parse_user_sql(sql)
        assert (result.source_db, result.source_table, result.where_clause) == expected

    def test_equality_on_a_timestamp_is_user_logic(self, rewriter):
        result = rewriter.parse_user_sql(
            "SELECT * FROM t WHERE _timestamp = '2026-01-01' AND x = 1"
        )
        assert result.where_clause == "_timestamp = '2026-01-01' AND x = 1"
        assert result.stripped_time_bounds == []

    def test_timestamp_columns_match_any_case_and_quoting(self):
        rewriter = RuleRewriter(timestamp_columns=frozenset({"Event_Time"}))
        result = rewriter.parse_user_sql(
            "SELECT * FROM t WHERE `EVENT_TIME` >= now() - INTERVAL 1 DAY AND x = 1"
        )
        assert result.where_clause == "x = 1"
        assert result.stripped_time_bounds == ["`EVENT_TIME` >= now() - INTERVAL 1 DAY"]

    def test_bound_under_or_is_reported(self, rewriter):
        result = rewriter.parse_user_sql("SELECT a FROM t WHERE _timestamp > now() OR b = 1")
        assert result.stripped_time_bounds == []
        assert any("stays in the rule" in w for w in result.warnings)

    def test_clauses_a_row_rule_cannot_carry_are_reported(self, rewriter):
        result = rewriter.parse_user_sql(
            "SELECT a, count() FROM t JOIN u ON t.id = u.id WHERE b = 1 GROUP BY a "
            "HAVING count() > 5 UNION ALL SELECT a, 1 FROM v WHERE c = 2"
        )
        assert result.source_table == "t"
        assert result.where_clause == "b = 1"
        assert "does not apply: JOIN, HAVING, UNION" in result.warnings[-1]

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT a FROM t WHERE b = 'unterminated",
            "SELECT a FROM t WHERE (b = 1",
            "INSERT INTO t SELECT * FROM u",
            "SELECT a FROM t WHERE",
            f"SELECT a FROM t WHERE {'(' * 3000}b = 1{')' * 3000}",
        ],
    )
    def test_unreadable_sql_is_a_warning_not_an_exception(self, rewriter, sql):
        result = rewriter.parse_user_sql(sql)
        assert result.source_table is None
        assert result.where_clause == ""
        assert result.warnings[0].startswith("Could not read the SQL: ")


# The placeholder-conjunct grammar as a plain pattern: the reference for what is dropped.
_REFERENCE_CONJUNCT = re.compile(
    r"\{timestamp_condition\}\s+AND\s+|\s+AND\s+\{timestamp_condition\}", re.IGNORECASE
)


def _reference_strip(where: str) -> str:
    return _REFERENCE_CONJUNCT.sub("", where).replace(TIME_PLACEHOLDER, "1").strip()


class TestStripTimePlaceholder:
    @pytest.mark.parametrize(
        "where",
        [
            "{timestamp_condition} AND x = 1",
            "x = 1 AND {timestamp_condition}",
            "x = 1  and\t{timestamp_condition} AND y = 2",
            "{timestamp_condition}",
            "(x = 1 OR {timestamp_condition})",
            "x = 1",
        ],
    )
    def test_strips_as_the_reference_grammar_does(self, where):
        assert strip_time_placeholder(where) == _reference_strip(where)

    def test_strips_as_the_reference_grammar_does_on_random_text(self):
        rng = random.Random(4701)
        tokens = [TIME_PLACEHOLDER, " ", " ", "\t", "AND", "and", "x", "{", "\n"]
        for _ in range(20_000):
            where = "".join(rng.choices(tokens, k=rng.randint(0, 10)))
            assert strip_time_placeholder(where) == _reference_strip(where), repr(where)

    @pytest.mark.parametrize(
        "where",
        [
            "a" + " " * 200_000 + "b",
            "a" + " " * 100_000 + "AND" + " " * 100_000 + "b",
        ],
        ids=["run", "run-and-run"],
    )
    def test_a_long_whitespace_run_is_read_in_linear_time(self, where):
        started = time.perf_counter()
        strip_time_placeholder(where)
        assert time.perf_counter() - started < 2.0
