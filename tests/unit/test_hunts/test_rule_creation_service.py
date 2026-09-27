"""Tests for RuleCreationService — HyperDX-aware rule creation pipeline."""

import pytest

from dfe_engine.hunts.rule_creation_service import (
    AIAnalysisStub,
    CostEstimate,
    RuleCreateRequest,
    RuleCreationService,
)


@pytest.fixture
def service():
    return RuleCreationService()


# ── RuleCreateRequest model ─────────────────────────────────


class TestRuleCreateRequest:
    """Test request model validation."""

    def test_basic_creation(self):
        req = RuleCreateRequest(
            name="Test Rule",
            user_sql="SELECT * FROM db.tbl WHERE x = 1",
        )
        assert req.name == "Test Rule"
        assert req.source_type == "raw"
        assert req.severity == "medium"

    def test_hyperdx_source_type(self):
        req = RuleCreateRequest(
            name="Test",
            source_type="hyperdx",
            user_sql="SELECT count() FROM default.logs WHERE x = 1",
        )
        assert req.source_type == "hyperdx"

    def test_requires_sql_or_cel(self):
        with pytest.raises(ValueError, match=r"user_sql.*cel_filter"):
            RuleCreateRequest(name="Test")

    def test_cel_only(self):
        req = RuleCreateRequest(
            name="Test",
            cel_filter='severity == "critical"',
            source="my_source",
        )
        assert req.cel_filter is not None
        assert req.user_sql is None

    def test_cost_window_default(self):
        req = RuleCreateRequest(
            name="Test",
            user_sql="SELECT 1 FROM db.tbl WHERE x = 1",
        )
        assert req.cost_window_minutes == 10
        assert req.estimate_cost is False


# ── Raw SQL rule creation ───────────────────────────────────


class TestCreateRuleRaw:
    """Test rule creation with source_type='raw'."""

    def test_basic_raw_creation(self, service):
        result = service.create_rule(
            RuleCreateRequest(
                name="Certutil Abuse",
                severity="high",
                user_sql="SELECT * FROM acme.windows_audit WHERE process_name = 'certutil.exe'",
            ),
            rule_id="win_cert_01",
        )
        assert result.rule.rule_id == "win_cert_01"
        assert result.rule.name == "Certutil Abuse"
        assert result.rule.severity == "high"
        assert "process_name" in result.rule.where_clause
        assert result.sanitize_summary == {}  # raw = no HyperDX sanitization

    def test_raw_strips_generic_time_bounds(self, service):
        """RuleRewriter still strips generic timestamp bounds for raw SQL."""
        result = service.create_rule(
            RuleCreateRequest(
                name="Test",
                user_sql=(
                    "SELECT * FROM db.tbl WHERE _timestamp > '2026-01-01' AND severity = 'high'"
                ),
            ),
            rule_id="r1",
        )
        assert "_timestamp" not in result.rule.where_clause
        assert "severity = 'high'" in result.rule.where_clause

    def test_raw_with_cel_filter(self, service):
        """CEL filter is transpiled and ANDed with SQL WHERE."""
        result = service.create_rule(
            RuleCreateRequest(
                name="Combined",
                user_sql="SELECT 1 FROM db.tbl WHERE process = 'cmd.exe'",
                cel_filter='severity == "critical"',
            ),
            rule_id="r2",
        )
        assert "process" in result.rule.where_clause
        assert "severity" in result.rule.where_clause

    def test_raw_preserves_source(self, service):
        result = service.create_rule(
            RuleCreateRequest(
                name="Test",
                user_sql="SELECT 1 FROM db.tbl WHERE x = 1",
                source="crowdstrike_edr",
            ),
            rule_id="r3",
        )
        assert result.rule.source == "crowdstrike_edr"


# ── HyperDX SQL rule creation ──────────────────────────────


class TestCreateRuleHyperDX:
    """Test rule creation with source_type='hyperdx'."""

    def test_strips_hyperdx_time_bounds(self, service):
        result = service.create_rule(
            RuleCreateRequest(
                name="HyperDX Test",
                source_type="hyperdx",
                user_sql=(
                    "SELECT count() FROM default.logs "
                    "WHERE (timestamp >= fromUnixTimestamp64Milli(1739318400000) "
                    "AND timestamp <= fromUnixTimestamp64Milli(1739491200000)) "
                    "AND severity = 'high'"
                ),
            ),
            rule_id="hdx1",
        )
        assert "fromUnixTimestamp64Milli" not in result.rule.where_clause
        assert "severity = 'high'" in result.rule.where_clause
        assert "stripped_time_bounds" in result.sanitize_summary

    def test_strips_settings_and_limit(self, service):
        result = service.create_rule(
            RuleCreateRequest(
                name="Settings Test",
                source_type="hyperdx",
                user_sql=(
                    "SELECT count() FROM default.logs WHERE x = 1 "
                    "LIMIT 10 OFFSET 0 "
                    "SETTINGS optimize_read_in_order = 0"
                ),
            ),
            rule_id="hdx2",
        )
        assert "SETTINGS" not in result.rule.original_sql
        assert "LIMIT" not in result.rule.original_sql
        assert "stripped_settings" in result.sanitize_summary
        assert "stripped_limit" in result.sanitize_summary

    def test_strips_time_bucket(self, service):
        result = service.create_rule(
            RuleCreateRequest(
                name="Bucket Test",
                source_type="hyperdx",
                user_sql=(
                    "SELECT countIf((ServiceName = 'api')),"
                    "toStartOfInterval(toDateTime(TimestampTime), INTERVAL 6 hour) "
                    "AS `__hdx_time_bucket` "
                    "FROM default.otel_logs "
                    "WHERE (TimestampTime >= fromUnixTimestamp64Milli(1741887731578) "
                    "AND TimestampTime <= fromUnixTimestamp64Milli(1742492531585)) "
                    "AND ((ServiceName = 'api')) "
                    "GROUP BY toStartOfInterval(toDateTime(TimestampTime), "
                    "INTERVAL 6 hour) AS `__hdx_time_bucket` "
                    "ORDER BY toStartOfInterval(toDateTime(TimestampTime), "
                    "INTERVAL 6 hour) AS `__hdx_time_bucket`"
                ),
            ),
            rule_id="hdx3",
        )
        assert "__hdx_time_bucket" not in result.rule.original_sql
        assert "toStartOfInterval" not in result.rule.original_sql
        assert "ServiceName = 'api'" in result.rule.where_clause
        assert result.sanitize_summary.get("had_time_bucket") is True

    def test_preserves_detection_logic(self, service):
        """All user detection logic is preserved after sanitization."""
        result = service.create_rule(
            RuleCreateRequest(
                name="Detection Logic",
                source_type="hyperdx",
                user_sql=(
                    "SELECT count(),severity FROM default.logs "
                    "WHERE (timestamp >= fromUnixTimestamp64Milli(1739318400000) "
                    "AND timestamp <= fromUnixTimestamp64Milli(1739491200000)) "
                    "AND process_name = 'certutil.exe' "
                    "AND severity IN ('high', 'critical') "
                    "GROUP BY severity "
                    "HAVING count(*) > 100 "
                    "SETTINGS optimize_read_in_order = 0"
                ),
            ),
            rule_id="hdx4",
        )
        assert "process_name" in result.rule.where_clause
        assert result.rule.source_table == "logs"

    def test_hyperdx_with_cel_filter(self, service):
        """HyperDX sanitization + CEL transpilation combined."""
        result = service.create_rule(
            RuleCreateRequest(
                name="HyperDX + CEL",
                source_type="hyperdx",
                user_sql=(
                    "SELECT * FROM default.logs "
                    "WHERE (timestamp >= fromUnixTimestamp64Milli(1739318400000) "
                    "AND timestamp <= fromUnixTimestamp64Milli(1739491200000)) "
                    "AND severity = 'high'"
                ),
                cel_filter='process_name == "certutil.exe"',
            ),
            rule_id="hdx5",
        )
        assert "fromUnixTimestamp64Milli" not in result.rule.where_clause
        assert "severity" in result.rule.where_clause
        # CEL transpiled and ANDed
        assert "process_name" in result.rule.where_clause


# ── SQL validation ──────────────────────────────────────────


class TestSqlValidation:
    """Test SQL syntax validation."""

    def test_valid_select(self, service):
        errors = service.validate_sql("SELECT count() FROM default.logs WHERE x = 1")
        assert errors == []

    def test_reject_non_select(self, service):
        errors = service.validate_sql("INSERT INTO logs VALUES (1, 2)")
        assert any("SELECT" in e.message for e in errors)

    def test_reject_missing_from(self, service):
        errors = service.validate_sql("SELECT 1 + 2")
        assert any("FROM" in e.message for e in errors)

    def test_reject_ddl(self, service):
        errors = service.validate_sql("SELECT 1 FROM logs; DROP TABLE logs")
        assert any("DROP" in e.message for e in errors)

    def test_unbalanced_parens(self, service):
        errors = service.validate_sql("SELECT count() FROM logs WHERE ((x = 1)")
        assert any("parenthesis" in e.message.lower() for e in errors)

    def test_unmatched_closing_paren(self, service):
        errors = service.validate_sql("SELECT count() FROM logs WHERE x = 1))")
        assert any("parenthesis" in e.message.lower() for e in errors)

    def test_reject_garbage_that_passes_the_keyword_checks(self, service):
        # SELECT and FROM both present, still not SQL.
        errors = service.validate_sql("SELECT 1SELECT 1 FROM dfe.main LIMIT 1")
        assert errors, "a query that does not parse must not validate"
        assert any("parse" in e.message.lower() for e in errors)

    def test_reject_two_statements(self, service):
        errors = service.validate_sql("SELECT 1 FROM logs; SELECT 2 FROM logs")
        assert any("one SELECT" in e.message for e in errors)

    def test_reject_dangling_operator(self, service):
        errors = service.validate_sql("SELECT count() FROM logs WHERE x = ")
        assert any("parse" in e.message.lower() for e in errors)

    def test_accepts_clickhouse_specific_syntax(self, service):
        sql = (
            "SELECT count() FROM dfe.main PREWHERE _source = 'x' "
            "WHERE _json.eventName = 'CreateUser' AND has(tags, 'a') "
            "SETTINGS max_threads = 2"
        )
        assert service.validate_sql(sql) == []

    def test_accepts_json_extract_and_final(self, service):
        sql = (
            "SELECT JSONExtractString(_json, 'user') AS u FROM dfe.main FINAL "
            "WHERE _timestamp_load > now() - INTERVAL 1 HOUR GROUP BY u LIMIT 10"
        )
        assert service.validate_sql(sql) == []

    def test_sql_errors_in_result(self, service):
        """Validation errors appear in RuleCreateResult."""
        result = service.create_rule(
            RuleCreateRequest(
                name="Bad SQL",
                user_sql="INSERT INTO logs VALUES (1)",
            ),
            rule_id="bad1",
        )
        assert len(result.sql_errors) > 0


# ── Cost estimation ─────────────────────────────────────────


class TestCostEstimation:
    """Test EXPLAIN cost estimation behavior."""

    def test_skipped_without_ch_config(self, service):
        """Cost estimation is skipped when no ClickHouse config."""
        result = service.create_rule(
            RuleCreateRequest(
                name="Test",
                user_sql="SELECT 1 FROM db.tbl WHERE x = 1",
                estimate_cost=True,
            ),
            rule_id="cost1",
        )
        # No ch_config → cost_estimate is None (no table extraction either)
        # or has warning if table was extracted
        assert result.cost_estimate is None or len(result.cost_estimate.warnings) > 0

    def test_skipped_without_source_table(self, service):
        """Cost estimation requires source_table on Rule."""
        result = service.create_rule(
            RuleCreateRequest(
                name="Test",
                cel_filter='x == "y"',
                estimate_cost=True,
            ),
            rule_id="cost2",
        )
        # No source table → cost estimation skipped
        assert result.cost_estimate is None

    def test_cost_estimate_model(self):
        """CostEstimate model structure."""
        ce = CostEstimate(
            estimated_rows=1000,
            explain_plan="Rows: 1000",
            explain_duration_ms=5.2,
            window_minutes=10,
        )
        assert ce.estimated_rows == 1000
        assert ce.window_minutes == 10


# ── AI analysis stub ────────────────────────────────────────


class TestAIAnalysisStub:
    """Test AI context generation."""

    def test_stub_populated(self, service):
        result = service.create_rule(
            RuleCreateRequest(
                name="AI Test",
                user_sql="SELECT * FROM db.tbl WHERE severity = 'high'",
            ),
            rule_id="ai1",
        )
        assert result.ai_context is not None
        assert result.ai_context.rule_id == "ai1"
        assert "severity" in result.ai_context.where_clause
        assert result.ai_context.source_table == "tbl"
        assert result.ai_context.source_db == "db"

    def test_stub_without_sql(self, service):
        result = service.create_rule(
            RuleCreateRequest(
                name="CEL Only",
                cel_filter='x == "y"',
            ),
            rule_id="ai2",
        )
        assert result.ai_context is not None
        assert result.ai_context.rule_id == "ai2"
        assert result.ai_context.original_sql == ""

    def test_stub_model_structure(self):
        stub = AIAnalysisStub(
            rule_id="test",
            original_sql="SELECT 1",
            clean_sql="SELECT 1",
            where_clause="x = 1",
            source_table="logs",
            cost_estimate=CostEstimate(window_minutes=10),
        )
        assert stub.cost_estimate is not None
        assert stub.cost_estimate.window_minutes == 10


# ── Sanitize summary ───────────────────────────────────────


class TestSanitizeSummary:
    """Test sanitize_summary in results."""

    def test_raw_has_empty_summary(self, service):
        result = service.create_rule(
            RuleCreateRequest(
                name="Raw",
                user_sql="SELECT 1 FROM db.tbl WHERE x = 1",
            ),
            rule_id="sum1",
        )
        assert result.sanitize_summary == {}

    def test_hyperdx_records_stripped_time_bounds(self, service):
        result = service.create_rule(
            RuleCreateRequest(
                name="HyperDX",
                source_type="hyperdx",
                user_sql=(
                    "SELECT 1 FROM default.logs "
                    "WHERE (timestamp >= fromUnixTimestamp64Milli(1739318400000) "
                    "AND timestamp <= fromUnixTimestamp64Milli(1739491200000)) "
                    "AND x = 1"
                ),
            ),
            rule_id="sum2",
        )
        assert "stripped_time_bounds" in result.sanitize_summary
        assert len(result.sanitize_summary["stripped_time_bounds"]) == 2

    def test_hyperdx_records_settings(self, service):
        result = service.create_rule(
            RuleCreateRequest(
                name="Settings",
                source_type="hyperdx",
                user_sql=(
                    "SELECT 1 FROM default.logs WHERE x = 1 SETTINGS optimize_read_in_order = 0"
                ),
            ),
            rule_id="sum3",
        )
        assert "stripped_settings" in result.sanitize_summary

    def test_no_patterns_clean_summary(self, service):
        """HyperDX mode with clean SQL produces empty summary."""
        result = service.create_rule(
            RuleCreateRequest(
                name="Clean",
                source_type="hyperdx",
                user_sql="SELECT 1 FROM default.logs WHERE severity = 'high'",
            ),
            rule_id="sum4",
        )
        assert result.sanitize_summary == {}
