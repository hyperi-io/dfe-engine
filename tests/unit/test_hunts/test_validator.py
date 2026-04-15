"""Tests for HuntValidator with Source model and scheduling field support."""

from unittest.mock import MagicMock

import pytest
from jinja2 import Environment, FileSystemLoader

from dfe_engine.hunts.validator import HuntValidator


@pytest.fixture
def rule_env(tmp_path):
    """Create a temp dir with a rule template and return (env, dir)."""
    rule_file = tmp_path / "test_rule.jinja2"
    rule_file.write_text("SELECT * FROM {{ source_table_name }} WHERE {{ timestamp_condition }}")
    env = Environment(loader=FileSystemLoader(str(tmp_path)), autoescape=True)
    return env, str(tmp_path)


def _base_hunt_data():
    """Minimal valid hunt config."""
    return {
        "name": "test_hunt",
        "log_buffer": 60,
        "global_source_table_name": "default.events",
        "global_target_table_name": "dfe_audit.results",
        "customers": ["org_a"],
        "cron": "*/5 * * * *",
        "rules": [{"rule_name": "test_rule", "initial_checkpoint_lookback_minutes": 10}],
    }


class TestValidatorSourceModel:
    """Test that validator accepts source-based configs."""

    def test_classic_config_passes(self, rule_env):
        """Traditional config with global_source_table_name validates."""
        env, rule_dir = rule_env
        data = _base_hunt_data()
        HuntValidator.validate_hunt_configuration(data, env, rule_dir, "timestamp_load")

    def test_source_ref_without_global_source(self, rule_env):
        """Config with per-rule source and no global_source_table_name validates."""
        env, rule_dir = rule_env
        data = _base_hunt_data()
        del data["global_source_table_name"]
        data["rules"][0]["source"] = "windows_audit"
        HuntValidator.validate_hunt_configuration(data, env, rule_dir, "timestamp_load")

    def test_missing_both_source_and_global_fails(self, rule_env):
        """Config with neither source nor global_source_table_name fails."""
        env, rule_dir = rule_env
        data = _base_hunt_data()
        del data["global_source_table_name"]
        # rules have no 'source' field either
        with pytest.raises(ValueError, match=r"global_source_table_name.*source"):
            HuntValidator.validate_hunt_configuration(data, env, rule_dir, "timestamp_load")

    def test_source_validated_against_registry(self, rule_env):
        """When source_registry is provided, unknown sources get a warning."""
        env, rule_dir = rule_env
        data = _base_hunt_data()
        data["rules"][0]["source"] = "nonexistent_source"

        registry = MagicMock()
        registry.get_source.side_effect = KeyError("not found")

        # Should not raise (warning only), so this should complete
        HuntValidator.validate_hunt_configuration(
            data, env, rule_dir, "timestamp_load", source_registry=registry
        )
        registry.get_source.assert_called_once_with("nonexistent_source")


class TestValidatorSchedulingFields:
    """Test validation of scheduling-related fields."""

    def test_valid_scheduling_mode_adaptive(self, rule_env):
        env, rule_dir = rule_env
        data = _base_hunt_data()
        data["scheduling_mode"] = "adaptive"
        HuntValidator.validate_hunt_configuration(data, env, rule_dir, "timestamp_load")

    def test_valid_scheduling_mode_cron(self, rule_env):
        env, rule_dir = rule_env
        data = _base_hunt_data()
        data["scheduling_mode"] = "cron"
        HuntValidator.validate_hunt_configuration(data, env, rule_dir, "timestamp_load")

    def test_invalid_scheduling_mode(self, rule_env):
        env, rule_dir = rule_env
        data = _base_hunt_data()
        data["scheduling_mode"] = "turbo"
        with pytest.raises(ValueError, match="scheduling_mode"):
            HuntValidator.validate_hunt_configuration(data, env, rule_dir, "timestamp_load")

    def test_valid_min_interval_seconds(self, rule_env):
        env, rule_dir = rule_env
        data = _base_hunt_data()
        data["min_interval_seconds"] = 120
        HuntValidator.validate_hunt_configuration(data, env, rule_dir, "timestamp_load")

    def test_negative_min_interval_fails(self, rule_env):
        env, rule_dir = rule_env
        data = _base_hunt_data()
        data["min_interval_seconds"] = -1
        with pytest.raises(ValueError, match="min_interval_seconds"):
            HuntValidator.validate_hunt_configuration(data, env, rule_dir, "timestamp_load")

    def test_no_scheduling_fields_ok(self, rule_env):
        """Omitting scheduling fields is fine (uses defaults)."""
        env, rule_dir = rule_env
        data = _base_hunt_data()
        HuntValidator.validate_hunt_configuration(data, env, rule_dir, "timestamp_load")
