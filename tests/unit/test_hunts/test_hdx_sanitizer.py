"""Tests for HdxSanitizer — HyperDX SQL pattern stripping.

Uses real HyperDX SQL samples from renderChartConfig.ts test snapshots.
"""

import pytest

from dfe_engine.hunts.hdx_sanitizer import HdxSanitizer


@pytest.fixture
def sanitizer():
    return HdxSanitizer()


# ── fromUnixTimestamp64Milli time bounds ─────────────────────


class TestEpochTimeBounds:
    """Strip fromUnixTimestamp64Milli() time bounds from WHERE."""

    def test_strips_gte_bound(self, sanitizer):
        sql = (
            "SELECT count() FROM default.logs "
            "WHERE (timestamp >= fromUnixTimestamp64Milli(1739318400000)) "
            "AND severity = 'high'"
        )
        result = sanitizer.sanitize(sql)
        assert "fromUnixTimestamp64Milli" not in result.clean_sql
        assert "severity = 'high'" in result.clean_sql
        assert len(result.stripped_time_bounds) >= 1

    def test_strips_lte_bound(self, sanitizer):
        sql = (
            "SELECT count() FROM default.logs "
            "WHERE severity = 'high' "
            "AND (timestamp <= fromUnixTimestamp64Milli(1739491200000))"
        )
        result = sanitizer.sanitize(sql)
        assert "fromUnixTimestamp64Milli" not in result.clean_sql
        assert "severity = 'high'" in result.clean_sql

    def test_strips_both_bounds(self, sanitizer):
        sql = (
            "SELECT count() FROM default.logs "
            "WHERE (timestamp >= fromUnixTimestamp64Milli(1739318400000) "
            "AND timestamp <= fromUnixTimestamp64Milli(1739491200000)) "
            "AND severity = 'high'"
        )
        result = sanitizer.sanitize(sql)
        assert "fromUnixTimestamp64Milli" not in result.clean_sql
        assert "severity = 'high'" in result.clean_sql
        assert len(result.stripped_time_bounds) == 2

    def test_preserves_non_timestamp_conditions(self, sanitizer):
        sql = (
            "SELECT count() FROM default.logs "
            "WHERE (timestamp >= fromUnixTimestamp64Milli(1739318400000) "
            "AND timestamp <= fromUnixTimestamp64Milli(1739491200000)) "
            "AND process_name = 'certutil.exe' "
            "AND severity IN ('high', 'critical')"
        )
        result = sanitizer.sanitize(sql)
        assert "process_name = 'certutil.exe'" in result.clean_sql
        assert "severity IN ('high', 'critical')" in result.clean_sql

    def test_todate_wrapped_variant(self, sanitizer):
        """toDate(fromUnixTimestamp64Milli(...)) for Date columns."""
        sql = (
            "SELECT count() FROM default.logs "
            "WHERE (event_date >= toDate(fromUnixTimestamp64Milli(1739318400000)) "
            "AND event_date <= toDate(fromUnixTimestamp64Milli(1739491200000))) "
            "AND x = 1"
        )
        result = sanitizer.sanitize(sql)
        assert "fromUnixTimestamp64Milli" not in result.clean_sql
        assert "toDate" not in result.clean_sql
        assert "x = 1" in result.clean_sql


# ── Compound time bounds ─────────────────────────────────────


class TestCompoundTimeBounds:
    """Strip toStartOfInterval(fromUnixTimestamp..., INTERVAL) patterns."""

    def test_simple_compound(self, sanitizer):
        sql = (
            "SELECT count() FROM default.metrics "
            "WHERE (TimeUnix >= toStartOfInterval("
            "fromUnixTimestamp64Milli(1739318400000), INTERVAL 2 minute) "
            "AND TimeUnix <= toStartOfInterval("
            "fromUnixTimestamp64Milli(1765670400000), INTERVAL 2 minute)) "
            "AND metric_name = 'cpu'"
        )
        result = sanitizer.sanitize(sql)
        assert "toStartOfInterval" not in result.clean_sql
        assert "fromUnixTimestamp64Milli" not in result.clean_sql
        assert "metric_name = 'cpu'" in result.clean_sql

    def test_compound_with_interval_offset(self, sanitizer):
        """Compound bounds with +/- INTERVAL offset."""
        sql = (
            "SELECT count() FROM default.metrics "
            "WHERE (TimeUnix >= toStartOfInterval("
            "fromUnixTimestamp64Milli(1739318400000), INTERVAL 2 minute) "
            "- INTERVAL 2 minute "
            "AND TimeUnix <= toStartOfInterval("
            "fromUnixTimestamp64Milli(1765670400000), INTERVAL 2 minute) "
            "+ INTERVAL 2 minute)"
        )
        result = sanitizer.sanitize(sql)
        assert "toStartOfInterval" not in result.clean_sql
        assert "fromUnixTimestamp64Milli" not in result.clean_sql
        assert "INTERVAL" not in result.clean_sql

    def test_compound_preserves_non_time_conditions(self, sanitizer):
        sql = (
            "SELECT count() FROM default.metrics "
            "WHERE (TimeUnix >= toStartOfInterval("
            "fromUnixTimestamp64Milli(1739318400000), INTERVAL 2 minute) "
            "AND TimeUnix <= toStartOfInterval("
            "fromUnixTimestamp64Milli(1765670400000), INTERVAL 2 minute)) "
            "AND ((MetricName = 'http.server.duration'))"
        )
        result = sanitizer.sanitize(sql)
        assert "MetricName = 'http.server.duration'" in result.clean_sql


# ── Time bucket in SELECT ────────────────────────────────────


class TestTimeBucketSelect:
    """Strip __hdx_time_bucket from SELECT clause."""

    def test_strips_time_bucket_select(self, sanitizer):
        sql = (
            "SELECT "
            "toStartOfInterval(toDateTime(TimestampTime), INTERVAL 6 hour) "
            "AS `__hdx_time_bucket`, "
            "count() "
            "FROM default.otel_logs WHERE x = 1"
        )
        result = sanitizer.sanitize(sql)
        assert "__hdx_time_bucket" not in result.clean_sql
        assert "toStartOfInterval" not in result.clean_sql
        assert "count()" in result.clean_sql
        assert result.had_time_bucket is True

    def test_preserves_other_select_columns(self, sanitizer):
        sql = (
            "SELECT "
            "severity, "
            "toStartOfInterval(toDateTime(TimestampTime), INTERVAL 6 hour) "
            "AS `__hdx_time_bucket`, "
            "count() "
            "FROM default.logs WHERE x = 1"
        )
        result = sanitizer.sanitize(sql)
        assert "severity" in result.clean_sql
        assert "count()" in result.clean_sql
        assert "__hdx_time_bucket" not in result.clean_sql

    def test_cleans_dangling_comma(self, sanitizer):
        """After removing time bucket from SELECT, no dangling commas."""
        sql = (
            "SELECT "
            "toStartOfInterval(toDateTime(TimestampTime), INTERVAL 6 hour) "
            "AS `__hdx_time_bucket`, "
            "count() "
            "FROM default.logs WHERE x = 1"
        )
        result = sanitizer.sanitize(sql)
        assert ",," not in result.clean_sql
        assert not result.clean_sql.startswith("SELECT ,")
        assert ", FROM" not in result.clean_sql


# ── Time bucket in GROUP BY / ORDER BY ───────────────────────


class TestTimeBucketGroupByOrderBy:
    """Strip __hdx_time_bucket from GROUP BY / ORDER BY."""

    def test_strips_from_group_by(self, sanitizer):
        sql = (
            "SELECT count() FROM default.logs "
            "WHERE x = 1 "
            "GROUP BY `__hdx_time_bucket`"
        )
        result = sanitizer.sanitize(sql)
        assert "__hdx_time_bucket" not in result.clean_sql
        assert "GROUP BY" not in result.clean_sql  # empty, so removed

    def test_strips_from_order_by(self, sanitizer):
        sql = (
            "SELECT count() FROM default.logs "
            "WHERE x = 1 "
            "ORDER BY `__hdx_time_bucket`"
        )
        result = sanitizer.sanitize(sql)
        assert "__hdx_time_bucket" not in result.clean_sql
        assert "ORDER BY" not in result.clean_sql

    def test_preserves_other_group_by_columns(self, sanitizer):
        sql = (
            "SELECT count(), severity FROM default.logs "
            "WHERE x = 1 "
            "GROUP BY severity, `__hdx_time_bucket`"
        )
        result = sanitizer.sanitize(sql)
        assert "__hdx_time_bucket" not in result.clean_sql
        assert "GROUP BY" in result.clean_sql
        assert "severity" in result.clean_sql

    def test_preserves_user_order_by(self, sanitizer):
        sql = (
            "SELECT count(), severity FROM default.logs "
            "WHERE x = 1 "
            "ORDER BY severity DESC, `__hdx_time_bucket`"
        )
        result = sanitizer.sanitize(sql)
        assert "__hdx_time_bucket" not in result.clean_sql
        assert "ORDER BY" in result.clean_sql
        assert "severity DESC" in result.clean_sql

    def test_strips_full_time_bucket_expr_in_group_by(self, sanitizer):
        """GROUP BY with full toStartOfInterval expression."""
        sql = (
            "SELECT count() FROM default.otel_logs "
            "WHERE x = 1 "
            "GROUP BY toStartOfInterval(toDateTime(TimestampTime), "
            "INTERVAL 6 hour) AS `__hdx_time_bucket`"
        )
        result = sanitizer.sanitize(sql)
        assert "toStartOfInterval" not in result.clean_sql
        assert "__hdx_time_bucket" not in result.clean_sql


# ── SETTINGS clause ──────────────────────────────────────────


class TestSettingsClause:
    """Strip SETTINGS clause."""

    def test_strips_settings(self, sanitizer):
        sql = (
            "SELECT count() FROM default.logs WHERE x = 1 "
            "SETTINGS optimize_read_in_order = 0"
        )
        result = sanitizer.sanitize(sql)
        assert "SETTINGS" not in result.clean_sql
        assert result.stripped_settings is not None
        assert "optimize_read_in_order" in result.stripped_settings

    def test_strips_multi_setting(self, sanitizer):
        sql = (
            "SELECT count() FROM default.logs WHERE x = 1 "
            "SETTINGS optimize_read_in_order = 0, "
            "cast_keep_nullable = 1, "
            "count_distinct_implementation = 'uniqCombined64'"
        )
        result = sanitizer.sanitize(sql)
        assert "SETTINGS" not in result.clean_sql
        assert "optimize_read_in_order" not in result.clean_sql
        assert "count_distinct_implementation" not in result.clean_sql

    def test_no_settings_is_noop(self, sanitizer):
        sql = "SELECT count() FROM default.logs WHERE x = 1"
        result = sanitizer.sanitize(sql)
        assert result.stripped_settings is None
        assert result.clean_sql == sql


# ── LIMIT/OFFSET ─────────────────────────────────────────────


class TestLimitOffset:
    """Strip LIMIT/OFFSET."""

    def test_strips_limit(self, sanitizer):
        sql = "SELECT count() FROM default.logs WHERE x = 1 LIMIT 10"
        result = sanitizer.sanitize(sql)
        assert "LIMIT" not in result.clean_sql
        assert result.stripped_limit == "LIMIT 10"

    def test_strips_limit_offset(self, sanitizer):
        sql = "SELECT count() FROM default.logs WHERE x = 1 LIMIT 10 OFFSET 20"
        result = sanitizer.sanitize(sql)
        assert "LIMIT" not in result.clean_sql
        assert "OFFSET" not in result.clean_sql
        assert result.stripped_limit == "LIMIT 10 OFFSET 20"

    def test_no_limit_is_noop(self, sanitizer):
        sql = "SELECT count() FROM default.logs WHERE x = 1"
        result = sanitizer.sanitize(sql)
        assert result.stripped_limit is None


# ── Full HyperDX queries (end-to-end) ───────────────────────


class TestFullHyperDXQueries:
    """End-to-end tests with real HyperDX SQL samples."""

    def test_count_by_severity(self, sanitizer):
        """Real HyperDX: count by severity with time bounds + SETTINGS."""
        sql = (
            "SELECT count(),severity FROM default.logs "
            "WHERE (timestamp >= fromUnixTimestamp64Milli(1739318400000) "
            "AND timestamp <= fromUnixTimestamp64Milli(1739491200000)) "
            "GROUP BY severity "
            "SETTINGS optimize_read_in_order = 0, cast_keep_nullable = 1, "
            "additional_result_filter = 'x != 2', "
            "count_distinct_implementation = 'uniqCombined64', "
            "async_insert_busy_timeout_min_ms = 20000"
        )
        result = sanitizer.sanitize(sql)
        assert "fromUnixTimestamp64Milli" not in result.clean_sql
        assert "SETTINGS" not in result.clean_sql
        assert "severity" in result.clean_sql
        assert "default.logs" in result.clean_sql
        assert "GROUP BY severity" in result.clean_sql

    def test_time_bucketed_aggregation(self, sanitizer):
        """Real HyperDX: time-bucketed count with all patterns."""
        sql = (
            "SELECT countIf((ServiceName = 'hdx-oss-dev-api')),"
            "toStartOfInterval(toDateTime(TimestampTime), INTERVAL 6 hour) "
            "AS `__hdx_time_bucket` "
            "FROM default.otel_logs "
            "WHERE (TimestampTime >= fromUnixTimestamp64Milli(1741887731578) "
            "AND TimestampTime <= fromUnixTimestamp64Milli(1742492531585)) "
            "AND ((ServiceName = 'hdx-oss-dev-api')) "
            "GROUP BY toStartOfInterval(toDateTime(TimestampTime), "
            "INTERVAL 6 hour) AS `__hdx_time_bucket` "
            "ORDER BY toStartOfInterval(toDateTime(TimestampTime), "
            "INTERVAL 6 hour) AS `__hdx_time_bucket`"
        )
        result = sanitizer.sanitize(sql)
        assert "__hdx_time_bucket" not in result.clean_sql
        assert "toStartOfInterval" not in result.clean_sql
        assert "fromUnixTimestamp64Milli" not in result.clean_sql
        assert "ServiceName = 'hdx-oss-dev-api'" in result.clean_sql
        assert "countIf" in result.clean_sql
        assert result.had_time_bucket is True
        assert len(result.stripped_time_bounds) >= 2

    def test_query_with_resource_attributes(self, sanitizer):
        """Real HyperDX: query with bracket notation attribute access."""
        sql = (
            "SELECT countIf(ResourceAttributes['telemetry.sdk.language'] = 'nodejs'),"
            "ResourceAttributes['telemetry.sdk.language'],"
            "toStartOfInterval(toDateTime(TimestampTime), INTERVAL 6 hour) "
            "AS `__hdx_time_bucket` "
            "FROM default.otel_logs "
            "WHERE (TimestampTime >= fromUnixTimestamp64Milli(1741887731578) "
            "AND TimestampTime <= fromUnixTimestamp64Milli(1742492531585)) "
            "AND (ResourceAttributes['telemetry.sdk.language'] = 'nodejs') "
            "GROUP BY ResourceAttributes['telemetry.sdk.language'],"
            "toStartOfInterval(toDateTime(TimestampTime), INTERVAL 6 hour) "
            "AS `__hdx_time_bucket` "
            "ORDER BY toStartOfInterval(toDateTime(TimestampTime), "
            "INTERVAL 6 hour) AS `__hdx_time_bucket`"
        )
        result = sanitizer.sanitize(sql)
        assert "__hdx_time_bucket" not in result.clean_sql
        assert "fromUnixTimestamp64Milli" not in result.clean_sql
        assert "ResourceAttributes['telemetry.sdk.language']" in result.clean_sql
        assert "countIf" in result.clean_sql

    def test_simple_select_with_order_and_limit(self, sanitizer):
        """Simple HyperDX log query: SELECT with ORDER BY + LIMIT."""
        sql = (
            "SELECT Timestamp,ServiceName,SeverityText,Body "
            "FROM default.otel_logs "
            "WHERE (Timestamp >= fromUnixTimestamp64Milli(1759756098000) "
            "AND Timestamp <= fromUnixTimestamp64Milli(1759756998000)) "
            "ORDER BY Timestamp DESC"
        )
        result = sanitizer.sanitize(sql)
        assert "fromUnixTimestamp64Milli" not in result.clean_sql
        assert "Timestamp" in result.clean_sql  # in SELECT, not time bound
        assert "ServiceName" in result.clean_sql
        assert "Body" in result.clean_sql

    def test_having_clause_preserved(self, sanitizer):
        """HAVING clause is user logic, must be preserved."""
        sql = (
            "SELECT count(),severity FROM default.logs "
            "WHERE (timestamp >= fromUnixTimestamp64Milli(1739318400000) "
            "AND timestamp <= fromUnixTimestamp64Milli(1739491200000)) "
            "GROUP BY severity "
            "HAVING count(*) > 100 "
            "SETTINGS optimize_read_in_order = 0"
        )
        result = sanitizer.sanitize(sql)
        assert "HAVING count(*) > 100" in result.clean_sql
        assert "GROUP BY severity" in result.clean_sql
        assert "SETTINGS" not in result.clean_sql


# ── Edge cases ───────────────────────────────────────────────


class TestEdgeCases:
    """Edge cases and boundary conditions."""

    def test_empty_sql(self, sanitizer):
        result = sanitizer.sanitize("")
        assert result.clean_sql == ""
        assert result.stripped_time_bounds == []

    def test_sql_without_hyperdx_patterns(self, sanitizer):
        """Standard SQL passes through unchanged."""
        sql = (
            "SELECT count(), severity FROM default.logs "
            "WHERE severity = 'high' GROUP BY severity"
        )
        result = sanitizer.sanitize(sql)
        assert result.clean_sql == sql
        assert result.stripped_time_bounds == []
        assert result.stripped_settings is None
        assert result.had_time_bucket is False

    def test_only_time_bounds_produces_empty_where(self, sanitizer):
        """If WHERE has only time bounds, WHERE is removed."""
        sql = (
            "SELECT count() FROM default.logs "
            "WHERE (timestamp >= fromUnixTimestamp64Milli(1739318400000) "
            "AND timestamp <= fromUnixTimestamp64Milli(1739491200000))"
        )
        result = sanitizer.sanitize(sql)
        assert "WHERE" not in result.clean_sql
        assert "default.logs" in result.clean_sql
