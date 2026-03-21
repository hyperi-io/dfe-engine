"""Tests for Rule model — Pydantic model for detection rule CRUD."""

import pytest

from dfe_engine.hunts.rule_model import Rule, RuleCreate

# ── RuleCreate ──────────────────────────────────────────────────


class TestRuleCreate:
    """Test RuleCreate input model."""

    def test_basic_creation(self):
        rc = RuleCreate(
            name="Certutil Abuse",
            severity="high",
            user_sql="SELECT * FROM acme.windows_audit WHERE process_name = 'certutil.exe'",
        )
        assert rc.name == "Certutil Abuse"
        assert rc.severity == "high"

    def test_default_severity(self):
        rc = RuleCreate(
            name="Test",
            user_sql="SELECT 1",
        )
        assert rc.severity == "medium"

    def test_invalid_severity(self):
        with pytest.raises(ValueError, match="Invalid severity"):
            RuleCreate(name="Test", severity="extreme", user_sql="SELECT 1")

    def test_severity_case_insensitive(self):
        rc = RuleCreate(name="Test", severity="HIGH", user_sql="SELECT 1")
        assert rc.severity == "high"

    def test_parse_extracts_table(self):
        rc = RuleCreate(
            name="Test",
            user_sql="SELECT * FROM acme.windows_audit WHERE x = 1",
        )
        parsed = rc.parse()
        assert parsed.source_db == "acme"
        assert parsed.source_table == "windows_audit"

    def test_parse_strips_time_bounds(self):
        rc = RuleCreate(
            name="Test",
            user_sql=(
                "SELECT * FROM db.tbl WHERE _timestamp > '2026-01-01' AND process_name = 'cmd.exe'"
            ),
        )
        parsed = rc.parse()
        assert "process_name" in parsed.where_clause
        assert "_timestamp" not in parsed.where_clause

    def test_optional_fields(self):
        rc = RuleCreate(
            name="Test",
            user_sql="SELECT 1",
            hunt_name="my_hunt",
            source="crowdstrike_edr",
        )
        assert rc.hunt_name == "my_hunt"
        assert rc.source == "crowdstrike_edr"


# ── Rule ────────────────────────────────────────────────────────


class TestRule:
    """Test Rule model creation and methods."""

    def test_from_create(self):
        rc = RuleCreate(
            name="Certutil Abuse",
            severity="high",
            user_sql="SELECT * FROM acme.windows_audit WHERE process_name = 'certutil.exe'",
            hunt_name="windows_hunt",
        )
        rule = Rule.from_create(rc, rule_id="win_cert_01")

        assert rule.rule_id == "win_cert_01"
        assert rule.name == "Certutil Abuse"
        assert rule.severity == "high"
        assert rule.source_db == "acme"
        assert rule.source_table == "windows_audit"
        assert "process_name" in rule.where_clause
        assert rule.had_select_star is True
        assert rule.hunt_name == "windows_hunt"
        assert len(rule.warnings) > 0  # SELECT * warning

    def test_from_create_preserves_source(self):
        rc = RuleCreate(
            name="Test",
            user_sql="SELECT 1 FROM db.tbl WHERE x = 1",
            source="my_source",
        )
        rule = Rule.from_create(rc, rule_id="r1")
        assert rule.source == "my_source"

    def test_to_rule_info(self):
        rule = Rule(
            rule_id="r1",
            name="Test Rule",
            source_table="windows_audit",
            source="crowdstrike_edr",
            where_clause="x = 1",
        )
        info = rule.to_rule_info()
        assert info["rule_name"] == "r1"
        assert info["source_table_name"] == "windows_audit"
        assert info["source"] == "crowdstrike_edr"

    def test_to_rule_info_minimal(self):
        rule = Rule(
            rule_id="r2",
            name="Minimal",
            where_clause="y = 2",
        )
        info = rule.to_rule_info()
        assert info["rule_name"] == "r2"
        assert "source_table_name" not in info
        assert "source" not in info

    def test_validate_valid_rule(self):
        rule = Rule(
            rule_id="r1",
            name="Good Rule",
            source_table="windows_audit",
            where_clause="process_name = 'cmd.exe'",
        )
        assert rule.validate_rule() == []

    def test_validate_empty_where(self):
        rule = Rule(
            rule_id="r1",
            name="Bad Rule",
            source_table="windows_audit",
            where_clause="",
        )
        errors = rule.validate_rule()
        assert any("empty detection" in e.lower() for e in errors)

    def test_validate_no_source_table(self):
        rule = Rule(
            rule_id="r1",
            name="No Table",
            where_clause="x = 1",
        )
        errors = rule.validate_rule()
        assert any("source table" in e.lower() for e in errors)

    def test_created_at_is_set(self):
        rule = Rule(
            rule_id="r1",
            name="Test",
            where_clause="x = 1",
        )
        assert rule.created_at  # Should be an ISO 8601 string

    def test_serialization_roundtrip(self):
        rc = RuleCreate(
            name="Roundtrip",
            severity="low",
            user_sql="SELECT 1 FROM db.tbl WHERE x > 1",
        )
        rule = Rule.from_create(rc, rule_id="rt_01")
        data = rule.model_dump(mode="json")
        rule2 = Rule.model_validate(data)
        assert rule2.rule_id == rule.rule_id
        assert rule2.where_clause == rule.where_clause
