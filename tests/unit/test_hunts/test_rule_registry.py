"""Tests for RuleRegistry YAML identity fields."""

from dfe_engine.hunts.rule_model import Rule
from dfe_engine.hunts.rule_registry import RuleRegistry


def test_rule_yaml_omits_file_name(tmp_path):
    reg = RuleRegistry(rules_directory=tmp_path, writable=True, refresh_interval=0)
    try:
        rule = Rule(
            rule_id="win_cert_01",
            name="Certutil Abuse",
            severity="high",
            where_clause="x = 1",
            original_sql="SELECT 1",
        )
        reg.save(rule)
        yaml_data = __import__("dfe_engine.yaml_utils", fromlist=["yaml_load"]).yaml_load(
            tmp_path / "win_cert_01.yaml"
        )
        assert "rule_id" not in yaml_data
        assert "name" not in yaml_data
        assert yaml_data["display_name"] == "Certutil Abuse"
        loaded = reg.get("win_cert_01")
        assert loaded.rule_id == "win_cert_01"
        assert loaded.name == "Certutil Abuse"
    finally:
        reg.close()
