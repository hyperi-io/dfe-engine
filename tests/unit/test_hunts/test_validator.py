"""Tests for HuntValidator with Source model and scheduling field support."""

from unittest.mock import MagicMock

import pytest
from scalo.logger import logger

from dfe_engine.hunts.validator import HuntValidator


@pytest.fixture
def warnings():
    """Every warning the validator logs while the test runs."""
    seen: list[str] = []
    handler = logger.add(
        lambda message: seen.append(message.record["message"]), level="WARNING", format="{message}"
    )
    yield seen
    logger.remove(handler)


@pytest.fixture
def rule_dir(tmp_path):
    """A rules directory holding ``test_rule.yaml``, as the rule registry writes it."""
    rule_file = tmp_path / "test_rule.yaml"
    rule_file.write_text("display_name: Test Rule\nwhere_clause: severity = 'high'\n")
    return str(tmp_path)


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

    def test_classic_config_passes(self, rule_dir):
        """Traditional config with global_source_table_name validates."""
        data = _base_hunt_data()
        HuntValidator.validate_hunt_configuration(data, rule_dir, "timestamp_load")

    def test_source_ref_without_global_source(self, rule_dir):
        """Config with per-rule source and no global_source_table_name validates."""
        data = _base_hunt_data()
        del data["global_source_table_name"]
        data["rules"][0]["source"] = "windows_audit"
        HuntValidator.validate_hunt_configuration(data, rule_dir, "timestamp_load")

    def test_missing_both_source_and_global_fails(self, rule_dir):
        """Config with neither source nor global_source_table_name fails."""
        data = _base_hunt_data()
        del data["global_source_table_name"]
        # rules have no 'source' field either
        with pytest.raises(ValueError, match=r"global_source_table_name.*source"):
            HuntValidator.validate_hunt_configuration(data, rule_dir, "timestamp_load")

    def test_source_validated_against_registry(self, rule_dir):
        """When source_registry is provided, unknown sources get a warning."""
        data = _base_hunt_data()
        data["rules"][0]["source"] = "nonexistent_source"

        registry = MagicMock()
        registry.get_source.side_effect = KeyError("not found")

        # Should not raise (warning only), so this should complete
        HuntValidator.validate_hunt_configuration(
            data, rule_dir, "timestamp_load", source_registry=registry
        )
        registry.get_source.assert_called_once_with("nonexistent_source")


class TestValidatorSchedulingFields:
    """Test validation of scheduling-related fields."""

    def test_valid_scheduling_mode_adaptive(self, rule_dir):
        data = _base_hunt_data()
        data["scheduling_mode"] = "adaptive"
        HuntValidator.validate_hunt_configuration(data, rule_dir, "timestamp_load")

    def test_valid_scheduling_mode_cron(self, rule_dir):
        data = _base_hunt_data()
        data["scheduling_mode"] = "cron"
        HuntValidator.validate_hunt_configuration(data, rule_dir, "timestamp_load")

    def test_invalid_scheduling_mode(self, rule_dir):
        data = _base_hunt_data()
        data["scheduling_mode"] = "turbo"
        with pytest.raises(ValueError, match="scheduling_mode"):
            HuntValidator.validate_hunt_configuration(data, rule_dir, "timestamp_load")

    def test_valid_min_interval_seconds(self, rule_dir):
        data = _base_hunt_data()
        data["min_interval_seconds"] = 120
        HuntValidator.validate_hunt_configuration(data, rule_dir, "timestamp_load")

    def test_negative_min_interval_fails(self, rule_dir):
        data = _base_hunt_data()
        data["min_interval_seconds"] = -1
        with pytest.raises(ValueError, match="min_interval_seconds"):
            HuntValidator.validate_hunt_configuration(data, rule_dir, "timestamp_load")

    def test_no_scheduling_fields_ok(self, rule_dir):
        """Omitting scheduling fields is fine (uses defaults)."""
        data = _base_hunt_data()
        HuntValidator.validate_hunt_configuration(data, rule_dir, "timestamp_load")


class TestValidatorRuleFile:
    """Each rule is checked as the ``{name}.yaml`` the hunt runner reads."""

    def test_an_existing_yaml_rule_validates_clean(self, rule_dir, warnings):
        HuntValidator.validate_hunt_configuration(_base_hunt_data(), rule_dir, "timestamp_load")

        assert not [w for w in warnings if "Rule file not found" in w]

    def test_a_missing_rule_warns_with_its_yaml_path(self, tmp_path, warnings):
        HuntValidator.validate_hunt_configuration(_base_hunt_data(), tmp_path, "timestamp_load")

        [warning] = [w for w in warnings if "Rule file not found" in w]
        assert str(tmp_path / "test_rule.yaml") in warning

    def test_a_jinja2_template_is_not_the_file_the_runner_reads(self, tmp_path, warnings):
        (tmp_path / "test_rule.jinja2").write_text("SELECT * FROM {{ source_table_name }}")

        HuntValidator.validate_hunt_configuration(_base_hunt_data(), tmp_path, "timestamp_load")

        assert [w for w in warnings if "Rule file not found" in w]

    def test_a_yaml_rule_is_not_parsed_as_a_jinja2_template(self, tmp_path):
        (tmp_path / "test_rule.yaml").write_text("where_clause: msg LIKE '{% raw'\n")

        HuntValidator.validate_hunt_configuration(_base_hunt_data(), tmp_path, "timestamp_load")

    @pytest.mark.parametrize(
        ("content", "reason"),
        [("- a\n- b\n", "not a YAML mapping"), ("a: [unclosed\n", "cannot be read as YAML")],
    )
    def test_a_rule_the_runner_cannot_read_is_refused(self, tmp_path, content, reason):
        (tmp_path / "test_rule.yaml").write_text(content)

        with pytest.raises(ValueError, match=reason):
            HuntValidator.validate_hunt_configuration(_base_hunt_data(), tmp_path, "timestamp_load")
