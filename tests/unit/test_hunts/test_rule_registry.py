"""Tests for RuleRegistry YAML identity fields."""

import pytest

from dfe_engine.hunts.rule_model import Rule
from dfe_engine.hunts.rule_registry import RuleNotFoundError, RuleRegistry


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


def test_deleting_a_name_outside_the_rules_dir_is_not_found_and_deletes_nothing(tmp_path):
    rules_dir = tmp_path / "rules"
    outside = tmp_path / "outside.yaml"
    outside.write_text("display_name: Outside\n", encoding="utf-8")
    reg = RuleRegistry(rules_directory=rules_dir, writable=True, refresh_interval=0)
    try:
        with pytest.raises(RuleNotFoundError):
            reg.delete("../outside")
    finally:
        reg.close()
    assert outside.is_file()


def test_saving_a_name_outside_the_rules_dir_is_refused_and_writes_nothing(tmp_path):
    rules_dir = tmp_path / "rules"
    reg = RuleRegistry(rules_directory=rules_dir, writable=True, refresh_interval=0)
    try:
        rule = Rule(rule_id="../escaped", name="Escaped", where_clause="x = 1")
        with pytest.raises(ValueError, match="rule name"):
            reg.save(rule)
    finally:
        reg.close()
    assert not (tmp_path / "escaped.yaml").exists()
