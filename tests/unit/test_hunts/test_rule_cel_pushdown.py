"""Tests for CEL-to-SQL query pushdown in the Rule model.

Verifies that CEL expressions are correctly transpiled to ClickHouse SQL
WHERE clauses and integrated into the hunt Rule model for query pushdown.

Three creation paths:
1. SQL only — existing behaviour, unchanged
2. CEL only — transpile_to_clickhouse() generates the WHERE clause
3. SQL + CEL — both are ANDed together
"""

import pytest

from dfe_engine.hunts.hunt_output import HuntResultSchema
from dfe_engine.hunts.rule_model import Rule, RuleCreate

# ── RuleCreate validation ────────────────────────────────────────


class TestRuleCreateValidation:
    """RuleCreate requires at least one of user_sql or cel_filter."""

    def test_neither_sql_nor_cel_raises(self):
        with pytest.raises(ValueError, match="At least one"):
            RuleCreate(name="Empty", severity="high")

    def test_sql_only_accepted(self):
        rc = RuleCreate(
            name="SQL Only",
            user_sql="SELECT * FROM db.tbl WHERE x = 1",
        )
        assert rc.user_sql is not None
        assert rc.cel_filter is None

    def test_cel_only_accepted(self):
        rc = RuleCreate(
            name="CEL Only",
            cel_filter='severity == "critical"',
            source="windows_audit",
        )
        assert rc.cel_filter is not None
        assert rc.user_sql is None

    def test_both_sql_and_cel_accepted(self):
        rc = RuleCreate(
            name="Both",
            user_sql="SELECT * FROM db.tbl WHERE x = 1",
            cel_filter='severity == "high"',
        )
        assert rc.user_sql is not None
        assert rc.cel_filter is not None

    def test_empty_strings_treated_as_missing(self):
        """Empty strings for both fields should fail validation."""
        with pytest.raises(ValueError, match="At least one"):
            RuleCreate(name="Empty Strings", user_sql="", cel_filter="")

    def test_parse_with_no_sql_returns_empty_parsed(self):
        rc = RuleCreate(
            name="CEL Only",
            cel_filter='status == "active"',
        )
        parsed = rc.parse()
        assert parsed.source_db is None
        assert parsed.source_table is None
        assert parsed.where_clause == ""


# ── CEL-only rules ───────────────────────────────────────────────


class TestCelOnlyRules:
    """Rules created from CEL expressions only (no user SQL)."""

    def test_simple_equality(self):
        rule = Rule.from_create(
            RuleCreate(
                name="Critical Severity",
                cel_filter='severity == "critical"',
                source="windows_audit",
            ),
            rule_id="crit_01",
        )
        assert rule.where_clause == "severity = 'critical'"
        assert rule.cel_filter == 'severity == "critical"'
        assert rule.source == "windows_audit"
        assert rule.original_sql == ""

    def test_negation(self):
        rule = Rule.from_create(
            RuleCreate(
                name="Not Test",
                cel_filter="!is_test",
                source="events",
            ),
            rule_id="neg_01",
        )
        assert rule.where_clause == "NOT is_test"

    def test_no_source_table_without_sql(self):
        """CEL-only rules have no source_table extracted from SQL."""
        rule = Rule.from_create(
            RuleCreate(
                name="CEL Only",
                cel_filter='x == "y"',
                source="my_source",
            ),
            rule_id="no_tbl_01",
        )
        assert rule.source_table is None
        assert rule.source == "my_source"


# A CEL filter and the SQL fragments its WHERE clause must contain.
CEL_TO_SQL = [
    (
        'severity == "critical" && amount > 10000',
        ("severity = 'critical'", "amount > 10000", "AND"),
    ),
    ('status == "error" || status == "failed"', ("status = 'error'", "status = 'failed'", "OR")),
    (
        'status in ["blocked", "banned", "suspended"]',
        ("IN", "'blocked'", "'banned'", "'suspended'"),
    ),
    ('process_name.contains("certutil")', ("position(process_name, 'certutil') > 0",)),
    ('path.startsWith("/admin")', ("startsWith(path, '/admin')",)),
    ('filename.endsWith(".exe")', ("endsWith(filename, '.exe')",)),
    ('src_ip.matches("^10\\\\.0\\\\.")', ("match(src_ip,",)),
    ("failed_count * 100 / total_count > 50", ("intDiv(failed_count * 100, total_count) > 50",)),
    ('is_admin ? severity == "low" : severity == "critical"', ("if(is_admin,",)),
    ("int(port_str) > 1024", ("toInt64(port_str) > 1024",)),
    ("double(score_str) >= 0.95", ("toFloat64(score_str) >= 0.95",)),
    ("size(message) > 1000", ("length(message) > 1000",)),
    ("is_active == true && is_deleted == false", ("is_active = 1", "is_deleted = 0")),
    (
        "a > 1 && b >= 2 && c < 3 && d <= 4 && e == 5 && f != 6",
        ("a > 1", "b >= 2", "c < 3", "d <= 4", "e = 5", "f != 6"),
    ),
    ('event.user.role == "admin"', ("event.user.role = 'admin'",)),
    ("_json.port == 8080", ("_json.port.:Int64 = 8080",)),
    ('_json.port == "8080"', ("_json.port.:String = '8080'",)),
    ('message == "it\'s a test"', ("message = ", "it\\'s a test")),
    (
        '(severity == "high" || severity == "critical") && (status == "open" || status == "in_progress")',
        ("severity = 'high'", "severity = 'critical'", "status = 'open'", "status = 'in_progress'"),
    ),
    (
        'count > 100 && ratio >= 0.5 && name == "test" && is_active == true',
        ("count > 100", "ratio >= 0.5", "name = 'test'", "is_active = 1"),
    ),
    ("a || b && c", ("OR", "AND")),
    ("(a || b) && c", ("(a OR b)", "AND")),
    ('!(status in ["blocked", "banned"])', ("NOT", "IN")),
    (
        'path.startsWith("/api") && path.contains("admin") && !path.endsWith(".css")',
        ("startsWith(path, '/api')", "position(path, 'admin') > 0", "endsWith(path, '.css')"),
    ),
]


@pytest.mark.parametrize(("cel", "fragments"), CEL_TO_SQL, ids=[cel for cel, _ in CEL_TO_SQL])
def test_a_cel_filter_transpiles_into_the_where_clause(cel, fragments):
    rule = Rule.from_create(
        RuleCreate(name="Transpile", cel_filter=cel, source="events"), rule_id="t_01"
    )

    for fragment in fragments:
        assert fragment in rule.where_clause


# ── SQL-only rules (backward compatibility) ──────────────────────


class TestSqlOnlyRules:
    """Existing SQL-only behaviour is preserved."""

    def test_sql_only_unchanged(self):
        rc = RuleCreate(
            name="Certutil Abuse",
            severity="high",
            user_sql="SELECT * FROM acme.windows_audit WHERE process_name = 'certutil.exe'",
        )
        rule = Rule.from_create(rc, rule_id="win_cert_01")
        assert "process_name" in rule.where_clause
        assert rule.cel_filter is None
        assert rule.source_db == "acme"
        assert rule.source_table == "windows_audit"

    def test_sql_time_bound_stripping(self):
        rc = RuleCreate(
            name="Test",
            user_sql=(
                "SELECT 1 FROM db.tbl WHERE _timestamp > '2026-01-01' AND process_name = 'cmd.exe'"
            ),
        )
        rule = Rule.from_create(rc, rule_id="ts_01")
        assert "_timestamp" not in rule.where_clause
        assert "process_name" in rule.where_clause

    def test_sql_select_star_warning(self):
        rc = RuleCreate(
            name="Star",
            user_sql="SELECT * FROM db.tbl WHERE x = 1",
        )
        rule = Rule.from_create(rc, rule_id="star_01")
        assert rule.had_select_star is True
        assert len(rule.warnings) > 0


# ── Combined SQL + CEL rules ────────────────────────────────────


class TestCombinedSqlCel:
    """Rules with both SQL and CEL — CEL is ANDed into the WHERE."""

    def test_sql_and_cel_anded(self):
        rc = RuleCreate(
            name="Combined",
            user_sql="SELECT * FROM db.tbl WHERE process_name = 'cmd.exe'",
            cel_filter='severity == "critical"',
        )
        rule = Rule.from_create(rc, rule_id="combo_01")
        assert "process_name = 'cmd.exe'" in rule.where_clause
        assert "severity = 'critical'" in rule.where_clause
        assert "AND" in rule.where_clause

    def test_combined_preserves_sql_metadata(self):
        rc = RuleCreate(
            name="Combined Meta",
            user_sql="SELECT * FROM acme.events WHERE x = 1",
            cel_filter="y > 10",
        )
        rule = Rule.from_create(rc, rule_id="meta_01")
        assert rule.source_db == "acme"
        assert rule.source_table == "events"
        assert rule.cel_filter == "y > 10"

    def test_combined_preserves_cel_original(self):
        cel_expr = 'severity == "critical" && amount > 10000'
        rc = RuleCreate(
            name="Preserved",
            user_sql="SELECT 1 FROM db.tbl WHERE status = 'active'",
            cel_filter=cel_expr,
        )
        rule = Rule.from_create(rc, rule_id="pres_01")
        assert rule.cel_filter == cel_expr

    def test_combined_complex_cel_with_sql(self):
        rc = RuleCreate(
            name="Complex Combo",
            user_sql="SELECT * FROM db.tbl WHERE event_type = 'login'",
            cel_filter=(
                'src_ip.startsWith("10.") && user_agent.contains("bot") && failed_attempts > 5'
            ),
        )
        rule = Rule.from_create(rc, rule_id="complex_01")
        wc = rule.where_clause
        assert "event_type = 'login'" in wc
        assert "startsWith(src_ip, '10.')" in wc
        assert "position(user_agent, 'bot') > 0" in wc
        assert "failed_attempts > 5" in wc

    def test_combined_sql_time_stripped_cel_kept(self):
        """Time bounds stripped from SQL, CEL filter added."""
        rc = RuleCreate(
            name="Time Strip",
            user_sql=(
                "SELECT 1 FROM db.tbl WHERE _timestamp >= '2026-01-01' AND event_type = 'error'"
            ),
            cel_filter="count > 100",
        )
        rule = Rule.from_create(rc, rule_id="strip_01")
        assert "_timestamp" not in rule.where_clause
        assert "event_type" in rule.where_clause
        assert "count > 100" in rule.where_clause


# ── Error handling ───────────────────────────────────────────────


class TestCelErrors:
    """Error handling for invalid CEL expressions."""

    def test_invalid_cel_syntax_raises_at_creation(self):
        """Invalid CEL syntax should raise during from_create()."""
        from scalo.expression import ExpressionError

        with pytest.raises(ExpressionError):
            Rule.from_create(
                RuleCreate(
                    name="Bad CEL",
                    cel_filter="== invalid syntax ==",
                    source="data",
                ),
                rule_id="bad_01",
            )

    def test_empty_cel_treated_as_none(self):
        """Empty cel_filter string should fail RuleCreate validation."""
        with pytest.raises(ValueError, match="At least one"):
            RuleCreate(name="Empty", cel_filter="")

    def test_disallowed_cel_function_raises(self):
        """DFE profile disallows map/filter/exists etc."""
        from scalo.expression import ExpressionError

        with pytest.raises(ExpressionError):
            Rule.from_create(
                RuleCreate(
                    name="Disallowed",
                    cel_filter="items.exists(x, x > 10)",
                    source="data",
                ),
                rule_id="dis_01",
            )


# ── validate_rule() ──────────────────────────────────────────────


class TestValidateRule:
    """Rule.validate_rule() checks for execution readiness."""

    def test_valid_cel_only_rule_with_source(self):
        rule = Rule.from_create(
            RuleCreate(
                name="Valid CEL",
                cel_filter="count > 0",
                source="events",
            ),
            rule_id="valid_01",
        )
        errors = rule.validate_rule()
        assert errors == []

    def test_valid_sql_rule(self):
        rule = Rule.from_create(
            RuleCreate(
                name="Valid SQL",
                user_sql="SELECT * FROM db.tbl WHERE x = 1",
            ),
            rule_id="valid_02",
        )
        errors = rule.validate_rule()
        assert errors == []

    def test_cel_only_no_source_fails_validation(self):
        """CEL-only without source name should fail (no table to scan)."""
        rule = Rule(
            rule_id="no_src_01",
            name="No Source",
            where_clause="x = 1",
            cel_filter="x > 0",
        )
        errors = rule.validate_rule()
        assert any("source" in e.lower() for e in errors)

    def test_cel_only_with_source_name_passes(self):
        """CEL-only with source name passes (resolved at execution time)."""
        rule = Rule(
            rule_id="src_01",
            name="Has Source",
            where_clause="x = 1",
            cel_filter="x > 0",
            source="my_source",
        )
        errors = rule.validate_rule()
        assert errors == []

    def test_empty_where_fails(self):
        rule = Rule(
            rule_id="empty_01",
            name="Empty Where",
            where_clause="  ",
            source="data",
        )
        errors = rule.validate_rule()
        assert any("empty detection" in e.lower() for e in errors)


# ── Serialization ────────────────────────────────────────────────


class TestSerialization:
    """JSON round-trip preserves CEL fields."""

    def test_cel_filter_survives_roundtrip(self):
        cel_expr = 'severity == "critical" && amount > 10000'
        rule = Rule.from_create(
            RuleCreate(
                name="Roundtrip",
                cel_filter=cel_expr,
                source="events",
            ),
            rule_id="rt_01",
        )
        data = rule.model_dump(mode="json")
        rule2 = Rule.model_validate(data)
        assert rule2.cel_filter == cel_expr
        assert rule2.where_clause == rule.where_clause

    def test_sql_plus_cel_roundtrip(self):
        rule = Rule.from_create(
            RuleCreate(
                name="Both RT",
                user_sql="SELECT 1 FROM db.tbl WHERE x = 1",
                cel_filter="y > 10",
            ),
            rule_id="both_rt_01",
        )
        data = rule.model_dump(mode="json")
        rule2 = Rule.model_validate(data)
        assert rule2.cel_filter == "y > 10"
        assert "x = 1" in rule2.where_clause
        assert "y > 10" in rule2.where_clause

    def test_sql_only_roundtrip_no_cel(self):
        rule = Rule.from_create(
            RuleCreate(
                name="SQL RT",
                user_sql="SELECT * FROM db.tbl WHERE z = 3",
            ),
            rule_id="sql_rt_01",
        )
        data = rule.model_dump(mode="json")
        rule2 = Rule.model_validate(data)
        assert rule2.cel_filter is None


# ── Integration with HuntResultSchema ────────────────────────────


class TestBuildInsertSelect:
    """CEL-transpiled WHERE clauses work with build_insert_select()."""

    def test_cel_where_in_insert_select(self):
        rule = Rule.from_create(
            RuleCreate(
                name="Insert Select",
                cel_filter='severity == "critical" && amount > 10000',
                source="payment_events",
            ),
            rule_id="is_01",
        )
        schema = HuntResultSchema()
        sql = schema.build_insert_select(
            target_db="acme",
            target_table="detection",
            source_db="acme",
            source_table="payment_events",
            where_clause=rule.where_clause,
            rule_id=rule.rule_id,
            rule_name=rule.name,
            hunt_name="payment_hunt",
            severity=rule.severity,
        )
        assert "INSERT INTO `acme`.`detection`" in sql
        assert "FROM `acme`.`payment_events`" in sql
        assert "severity = 'critical'" in sql
        assert "amount > 10000" in sql
        assert "{timestamp_condition}" in sql

    def test_combined_where_in_insert_select(self):
        rule = Rule.from_create(
            RuleCreate(
                name="Combined IS",
                user_sql="SELECT * FROM db.events WHERE event_type = 'login'",
                cel_filter="failed_count > 3",
            ),
            rule_id="cis_01",
        )
        schema = HuntResultSchema()
        sql = schema.build_insert_select(
            target_db="acme",
            target_table="detection",
            source_db="acme",
            source_table="events",
            where_clause=rule.where_clause,
            rule_id=rule.rule_id,
            rule_name=rule.name,
            hunt_name="login_hunt",
            severity=rule.severity,
        )
        assert "event_type = 'login'" in sql
        assert "failed_count > 3" in sql
        assert "AND" in sql

    def test_cel_only_empty_sql_where(self):
        """CEL-only rule: no SQL WHERE to AND with."""
        rule = Rule.from_create(
            RuleCreate(
                name="Pure CEL",
                cel_filter="error_count > 0",
                source="logs",
            ),
            rule_id="pure_01",
        )
        schema = HuntResultSchema()
        sql = schema.build_insert_select(
            target_db="org1",
            target_table="results",
            source_db="org1",
            source_table="logs",
            where_clause=rule.where_clause,
            rule_id=rule.rule_id,
            rule_name=rule.name,
            hunt_name="error_hunt",
            severity=rule.severity,
        )
        assert "error_count > 0" in sql
        # Should have timestamp_condition AND the CEL filter
        assert "{timestamp_condition}" in sql


# ── Real-world detection patterns ────────────────────────────────


class TestRealWorldPatterns:
    """Realistic detection rules using CEL query pushdown."""

    def test_windows_certutil_abuse(self):
        rule = Rule.from_create(
            RuleCreate(
                name="Certutil Abuse Detection",
                severity="high",
                cel_filter=(
                    'process_name.contains("certutil") && '
                    '(cmdline.contains("-urlcache") || cmdline.contains("-decode"))'
                ),
                source="windows_audit",
            ),
            rule_id="T1140_certutil",
        )
        wc = rule.where_clause
        assert "position(process_name, 'certutil') > 0" in wc
        assert "position(cmdline, '-urlcache') > 0" in wc
        assert "position(cmdline, '-decode') > 0" in wc

    def test_brute_force_detection(self):
        rule = Rule.from_create(
            RuleCreate(
                name="Brute Force Login",
                severity="critical",
                cel_filter=(
                    'event_type == "authentication" && outcome == "failure" && failed_count > 10'
                ),
                source="auth_logs",
            ),
            rule_id="brute_force_01",
        )
        wc = rule.where_clause
        assert "event_type = 'authentication'" in wc
        assert "outcome = 'failure'" in wc
        assert "failed_count > 10" in wc

    def test_data_exfiltration_indicator(self):
        rule = Rule.from_create(
            RuleCreate(
                name="Large Outbound Transfer",
                severity="high",
                cel_filter=(
                    "bytes_out > 1000000 && "
                    '!dst_ip.startsWith("10.") && '
                    '!dst_ip.startsWith("192.168.")'
                ),
                source="network_flows",
            ),
            rule_id="exfil_01",
        )
        wc = rule.where_clause
        assert "bytes_out > 1000000" in wc
        assert "NOT startsWith(dst_ip, '10.')" in wc
        assert "NOT startsWith(dst_ip, '192.168.')" in wc

    def test_suspicious_powershell(self):
        rule = Rule.from_create(
            RuleCreate(
                name="Suspicious PowerShell",
                severity="high",
                cel_filter=(
                    'process_name.endsWith("powershell.exe") && '
                    '(cmdline.contains("-enc") || cmdline.contains("-nop") || '
                    'cmdline.contains("downloadstring"))'
                ),
                source="windows_audit",
            ),
            rule_id="T1059_ps",
        )
        wc = rule.where_clause
        assert "endsWith(process_name, 'powershell.exe')" in wc
        assert "position(cmdline, '-enc') > 0" in wc

    def test_sql_injection_attempt(self):
        """Combined: SQL extracts table, CEL adds the detection filter."""
        rule = Rule.from_create(
            RuleCreate(
                name="SQL Injection Attempt",
                severity="critical",
                user_sql="SELECT * FROM prod.web_access_logs WHERE status_code >= 400",
                cel_filter=(
                    'request_uri.contains("UNION") || '
                    'request_uri.contains("\'--") || '
                    'request_uri.contains("OR 1=1")'
                ),
            ),
            rule_id="sqli_01",
        )
        assert rule.source_table == "web_access_logs"
        assert "status_code >= 400" in rule.where_clause
        assert "UNION" in rule.where_clause
