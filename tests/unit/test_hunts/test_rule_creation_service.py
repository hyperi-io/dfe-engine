"""Tests for RuleCreationService — HyperDX-aware rule creation pipeline."""

import pytest

from dfe_engine.hunts.rule_creation_service import (
    AIAnalysisStub,
    CostEstimate,
    RuleCreateRequest,
    RuleCreationService,
)
from dfe_engine.hunts.rule_guard import VolumeBand
from dfe_engine.settings import DetectionGuardSettings, HuntsSettings


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
        assert req.cost_window_minutes is None
        assert req.estimate_cost is True


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

    def test_raw_literal_and_quoted_table_reach_the_rule_whole(self, service):
        """A raw rule's literal holding clause words and a quoted table both survive."""
        result = service.create_rule(
            RuleCreateRequest(
                name="Literal",
                user_sql=(
                    "SELECT * FROM `dfe`.`win-events` WHERE msg = 'x ORDER BY y; LIMIT 1' "
                    "AND _timestamp > now() - INTERVAL 1 DAY SETTINGS max_threads = 2"
                ),
            ),
            rule_id="r4",
        )
        assert result.sql_errors == []
        assert (result.rule.source_db, result.rule.source_table) == ("dfe", "win-events")
        assert result.rule.where_clause == "msg = 'x ORDER BY y; LIMIT 1'"

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
        assert result.sql_errors == []
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
        assert result.sql_errors == []
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
        assert result.sql_errors == []
        assert "__hdx_time_bucket" not in result.rule.original_sql
        assert "toStartOfInterval" not in result.rule.original_sql
        assert "ServiceName = 'api'" in result.rule.where_clause
        assert result.sanitize_summary.get("had_time_bucket") is True

    def test_preserves_detection_logic(self, service):
        """HAVING cannot become a row filter, so the request is refused, not widened."""
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
        assert [e.message.split(",")[0] for e in result.sql_errors] == [
            "HAVING filters aggregated groups"
        ]
        assert result.sanitize_summary == {}

    def test_preserves_every_detection_predicate(self, service):
        """Every user predicate survives; the window, grouping and SETTINGS do not."""
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
                    "SETTINGS optimize_read_in_order = 0"
                ),
            ),
            rule_id="hdx6",
        )
        assert result.sql_errors == []
        assert result.rule.where_clause == (
            "process_name = 'certutil.exe' AND severity IN ('high', 'critical')"
        )
        assert result.rule.source_db == "default"
        assert result.rule.source_table == "logs"

    def test_literal_holding_clause_words_reaches_the_rule_whole(self, service):
        """A search term that spells ORDER BY is data, end to end."""
        result = service.create_rule(
            RuleCreateRequest(
                name="Literal",
                source_type="hyperdx",
                user_sql=(
                    "SELECT _timestamp,_json FROM dfe.main WHERE (_timestamp >= "
                    "fromUnixTimestamp64Milli(1) AND _timestamp <= fromUnixTimestamp64Milli(2)) "
                    "AND ((toString(`_json`.`message`) = 'failed ORDER BY LIMIT 5; HAVING x')) "
                    "ORDER BY _timestamp DESC LIMIT 200"
                ),
            ),
            rule_id="hdx7",
        )
        assert result.sql_errors == []
        assert result.rule.where_clause == (
            "((toString(`_json`.`message`) = 'failed ORDER BY LIMIT 5; HAVING x'))"
        )

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
        assert result.sql_errors == []
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

    def test_raw_sql_with_having_is_refused_not_cut_short(self, service):
        """Stored as its WHERE alone, the rule would fire on every failed row, not six."""
        sql = "SELECT user, count() FROM dfe.main WHERE failed = 1 GROUP BY user HAVING count() = 6"
        result = service.create_rule(RuleCreateRequest(name="Threshold", user_sql=sql), rule_id="r1")
        assert [e.message.split(",")[0] for e in result.sql_errors] == [
            "HAVING filters aggregated groups"
        ]

    @pytest.mark.parametrize(
        ("sql", "refusal"),
        [
            (
                "SELECT * FROM dfe.main AS a JOIN dfe.other AS b ON a.id = b.id WHERE a.x = 1",
                "The query joins tables",
            ),
            (
                "SELECT * FROM dfe.main WHERE x = 1 UNION ALL SELECT * FROM dfe.main WHERE y = 2",
                "The query unions several SELECTs",
            ),
        ],
    )
    def test_raw_sql_refuses_what_the_hyperdx_path_refuses(self, service, sql, refusal):
        errors = service.validate_sql(sql)
        assert any(e.message.startswith(refusal) for e in errors), errors

    def test_raw_sql_grouping_without_having_is_a_row_filter(self, service):
        """The grouping only shapes the view; the rule matches the rows the WHERE keeps."""
        sql = "SELECT host, count() FROM dfe.main WHERE failed = 1 GROUP BY host"
        result = service.create_rule(RuleCreateRequest(name="Grouped", user_sql=sql), rule_id="r2")
        assert result.sql_errors == []
        assert result.rule.where_clause == "failed = 1"

    def test_the_time_placeholder_is_stripped_at_save(self, service):
        sql = "SELECT * FROM dfe.main WHERE {timestamp_condition} AND (action = 'delete')"
        result = service.create_rule(RuleCreateRequest(name="Sigma", user_sql=sql), rule_id="r4")
        assert result.sql_errors == []
        assert result.rule.where_clause == "(action = 'delete')"
        assert "{timestamp_condition}" not in result.rule.original_sql

    def test_a_bare_where_fragment_is_refused(self, service):
        result = service.create_rule(
            RuleCreateRequest(name="Fragment", user_sql="failed = 1"), rule_id="r3"
        )
        assert result.sql_errors
        assert "Rule has empty detection logic (WHERE clause)." in result.rule.validate_rule()

    @pytest.mark.parametrize(
        "condition",
        [
            "action = 'delete'",
            "operation IN ('CREATE', 'UPDATE', 'DROP')",
            "message LIKE '%(%'",
            "message = 'it''s (unbalanced'",
            "`drop` = 1",
        ],
    )
    def test_a_keyword_or_paren_inside_a_literal_is_data(self, service, condition):
        assert service.validate_sql(f"SELECT * FROM dfe.main WHERE {condition}") == []

    def test_reject_a_from_with_no_database(self, service):
        errors = service.validate_sql("SELECT * FROM events WHERE severity = 'high'")
        assert [e.message for e in errors] == [
            "SQL names no <db>.<table> source for the hunt runner to scan."
        ]

    def test_reject_a_templated_table(self, service):
        errors = service.validate_sql(
            "SELECT _timestamp,_json FROM {{org_id}}.{{source_table_name}} "
            "WHERE ({{timestamp_condition}}) ORDER BY _timestamp DESC"
        )
        assert errors, "a table left as a template placeholder must not validate"

    def test_hyperdx_sql_that_sanitises_to_nothing_is_an_error(self, service):
        result = service.create_rule(
            RuleCreateRequest(name="Nothing", source_type="hyperdx", user_sql="LIMIT 100"),
            rule_id="nothing",
        )
        assert result.sql_errors

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


_NO_CLICKHOUSE = (
    "The alert-volume preview could not finish: no ClickHouse is configured. "
    "This rule's alert volume is unknown until it runs."
)


class TestVolumePreview:
    """The preview's outcome when it cannot reach ClickHouse, and when it must not run."""

    def test_without_clickhouse_the_volume_is_unknown_not_quiet(self, service):
        result = service.create_rule(
            RuleCreateRequest(name="Test", user_sql="SELECT 1 FROM db.tbl WHERE x = 1"),
            rule_id="cost1",
        )

        assert result.cost_estimate is not None
        assert result.cost_estimate.band == VolumeBand.UNMEASURED
        assert result.cost_estimate.estimated_rows is None
        assert result.cost_estimate.window_minutes == 60
        assert result.cost_estimate.warnings == [_NO_CLICKHOUSE]
        assert _NO_CLICKHOUSE in result.rule.warnings

    def test_opting_out_of_the_numbers_keeps_the_warning(self, service):
        result = service.create_rule(
            RuleCreateRequest(
                name="Test", user_sql="SELECT 1 FROM db.tbl WHERE x = 1", estimate_cost=False
            ),
            rule_id="cost1b",
        )

        assert result.cost_estimate is None
        assert _NO_CLICKHOUSE in result.rule.warnings

    def test_a_disabled_guard_warns_nothing_and_measures_only_on_request(self):
        hunts = HuntsSettings(detection_guard=DetectionGuardSettings(enabled=False))
        service = RuleCreationService(hunts=hunts)

        quiet = service.create_rule(
            RuleCreateRequest(
                name="Test", user_sql="SELECT 1 FROM db.tbl WHERE x = 1", estimate_cost=False
            ),
            rule_id="cost1c",
        )
        asked = service.create_rule(
            RuleCreateRequest(name="Test", user_sql="SELECT 1 FROM db.tbl WHERE x = 1"),
            rule_id="cost1d",
        )

        assert quiet.cost_estimate is None
        assert asked.cost_estimate is not None
        assert _NO_CLICKHOUSE not in quiet.rule.warnings + asked.rule.warnings

    def test_a_zero_window_takes_the_deployment_window(self):
        hunts = HuntsSettings(detection_guard=DetectionGuardSettings(preview_window_minutes=15))
        result = RuleCreationService(hunts=hunts).create_rule(
            RuleCreateRequest(
                name="Test", user_sql="SELECT 1 FROM db.tbl WHERE x = 1", cost_window_minutes=0
            ),
            rule_id="cost1e",
        )

        assert result.cost_estimate is not None
        assert result.cost_estimate.window_minutes == 15

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

    def test_a_rule_that_matches_everything_is_named_and_never_measured(self, service):
        result = service.create_rule(
            RuleCreateRequest(
                name="All", user_sql="SELECT * FROM dfe.main WHERE notEmpty(_json) = 1"
            ),
            rule_id="all1",
        )

        assert result.matches_everything is not None
        assert result.matches_everything.startswith("This rule matches every event in dfe.main")
        assert result.cost_estimate is None
        assert _NO_CLICKHOUSE not in result.rule.warnings

    def test_an_empty_filter_is_left_to_the_execution_check(self, service):
        result = service.create_rule(
            RuleCreateRequest(name="Bare", user_sql="SELECT * FROM dfe.main"),
            rule_id="bare1",
        )

        assert result.rule.validate_rule() == ["Rule has empty detection logic (WHERE clause)."]
        assert result.matches_everything is None
        assert result.cost_estimate is None

    def test_cost_estimate_model(self):
        """CostEstimate model structure."""
        ce = CostEstimate(estimated_rows=1000, rows_in_window=4000, window_minutes=10)
        assert ce.estimated_rows == 1000
        assert ce.window_minutes == 10
        assert ce.band == VolumeBand.UNMEASURED


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
        assert result.sql_errors == []
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
        assert result.sql_errors == []
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
        # A refusal also leaves the summary empty, so the clean result is checked first.
        assert result.sql_errors == []
        assert result.sanitize_summary == {}
