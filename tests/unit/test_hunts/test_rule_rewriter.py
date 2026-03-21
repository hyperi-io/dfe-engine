"""Tests for RuleRewriter — SQL parsing and time bound stripping."""

import pytest

from dfe_engine.hunts.rule_rewriter import ParsedRule, RuleRewriter


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
