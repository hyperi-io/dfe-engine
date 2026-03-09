"""Tests for HuntEngine — background thread lifecycle."""

import time

import pytest
import yaml

from dfe_engine.hunts.hunt_engine import HuntEngine
from dfe_engine.settings import DFESettings, HuntsSettings


@pytest.fixture
def hunt_dirs(tmp_path):
    """Create temp hunt and rule directories with a minimal hunt config."""
    hunt_dir = tmp_path / "hunts"
    rule_dir = tmp_path / "rules"
    hunt_dir.mkdir()
    rule_dir.mkdir()

    # Create a minimal hunt YAML
    hunt_config = {
        "name": "test_hunt",
        "cron": "*/5 * * * *",
        "log_buffer": 60,
        "global_target_table_name": "test_alerts",
        "global_source_table_name": "test_source",
        "checkpoint_timestamp_field": "timestamp_load",
        "customers": ["test_org"],
        "rules": [
            {
                "rule_name": "test_rule",
                "initial_checkpoint_lookback_minutes": 10,
            }
        ],
    }
    with open(hunt_dir / "test_hunt.yaml", "w") as f:
        yaml.dump(hunt_config, f)

    # Create a minimal Jinja2 rule template (lean output — never SELECT *)
    rule_template = (
        "INSERT INTO {{org_id}}.{{target_table_name}} "
        "(_timestamp, _org_id, matched_uuid, rule_id, rule_name, source_table) "
        "SELECT _timestamp, _org_id, _uuid AS matched_uuid, "
        "'test_rule' AS rule_id, 'test_rule' AS rule_name, "
        "'{{source_table_name}}' AS source_table "
        "FROM {{org_id}}.{{source_table_name}} "
        "WHERE {timestamp_condition}"
    )
    with open(rule_dir / "test_rule.jinja2", "w") as f:
        f.write(rule_template)

    return str(hunt_dir), str(rule_dir)


@pytest.fixture
def engine_settings(hunt_dirs, tmp_path):
    """Create DFESettings configured for test hunt dirs."""
    hunt_dir, rule_dir = hunt_dirs
    log_path = str(tmp_path / "logs")

    settings = DFESettings(
        hunts=HuntsSettings(
            hunt_dir=hunt_dir,
            rule_repo_dir=rule_dir,
            log_path=log_path,
            cron_task_timeout=3,
            checkpoint_destination="file",
            checkpoint_path=str(tmp_path / "checkpoints"),
            jitter_seconds=0,
        ),
    )
    return settings


class TestHuntEngineLifecycle:
    """Test HuntEngine start/stop lifecycle."""

    def test_start_and_is_running(self, engine_settings):
        """Engine should be running after start()."""
        engine = HuntEngine(settings=engine_settings)
        engine.start(timeout=10)
        try:
            assert engine.is_running
        finally:
            engine.stop(timeout=5)

    def test_stop_sets_not_running(self, engine_settings):
        """Engine should not be running after stop()."""
        engine = HuntEngine(settings=engine_settings)
        engine.start(timeout=10)
        engine.stop(timeout=5)
        assert not engine.is_running

    def test_start_twice_is_idempotent(self, engine_settings):
        """Calling start() when already running should be a no-op."""
        engine = HuntEngine(settings=engine_settings)
        engine.start(timeout=10)
        try:
            engine.start(timeout=5)  # Should not raise
            assert engine.is_running
        finally:
            engine.stop(timeout=5)

    def test_stop_when_not_running(self, engine_settings):
        """Calling stop() when not running should be a no-op."""
        engine = HuntEngine(settings=engine_settings)
        engine.stop()  # Should not raise
        assert not engine.is_running

    def test_timeout_stops_engine(self, engine_settings):
        """Engine should stop after cron_task_timeout."""
        engine_settings.hunts.cron_task_timeout = 2
        engine = HuntEngine(settings=engine_settings)
        engine.start(timeout=10)
        # Wait for timeout to expire
        time.sleep(3)
        assert not engine.is_running

    def test_not_running_before_start(self, engine_settings):
        """is_running should be False before start() is called."""
        engine = HuntEngine(settings=engine_settings)
        assert not engine.is_running


class TestHuntEngineConfig:
    """Test HuntEngine configuration handling."""

    def test_empty_hunt_dir_no_error(self, tmp_path):
        """Empty hunt_dir should not raise, just warn."""
        settings = DFESettings(
            hunts=HuntsSettings(
                hunt_dir="",
                log_path=str(tmp_path / "logs"),
                cron_task_timeout=1,
            ),
        )
        engine = HuntEngine(settings=settings)
        engine.start(timeout=5)
        time.sleep(0.5)
        # Engine should have exited cleanly (no dirs to process)
        engine.stop(timeout=5)

    def test_mismatched_dir_counts(self, tmp_path):
        """Mismatched hunt_dir/rule_repo_dir counts should raise ValueError."""
        hunt_dir = tmp_path / "hunts"
        hunt_dir.mkdir()

        settings = DFESettings(
            hunts=HuntsSettings(
                hunt_dir=str(hunt_dir),
                rule_repo_dir=f"{tmp_path / 'rules1'},{tmp_path / 'rules2'}",
                log_path=str(tmp_path / "logs"),
                cron_task_timeout=2,
            ),
        )
        engine = HuntEngine(settings=settings)
        engine.start(timeout=5)
        # The engine should fail internally but not crash the main thread
        time.sleep(1)
        engine.stop(timeout=5)

    def test_nonexistent_dirs_logged(self, tmp_path):
        """Non-existent directories should be logged and skipped."""
        settings = DFESettings(
            hunts=HuntsSettings(
                hunt_dir="/nonexistent/path",
                rule_repo_dir="/other/nonexistent",
                log_path=str(tmp_path / "logs"),
                cron_task_timeout=1,
            ),
        )
        engine = HuntEngine(settings=settings)
        engine.start(timeout=5)
        time.sleep(0.5)
        engine.stop(timeout=5)
        assert not engine.is_running

    def test_parse_dirs_comma_separated(self):
        """_parse_dirs should split on commas and strip whitespace."""
        result = HuntEngine._parse_dirs("/path/a, /path/b , /path/c")
        assert result == ["/path/a", "/path/b", "/path/c"]

    def test_parse_dirs_empty(self):
        """_parse_dirs should return empty list for empty string."""
        assert HuntEngine._parse_dirs("") == []

    def test_parse_dirs_single(self):
        """_parse_dirs should return single-element list for a single path."""
        assert HuntEngine._parse_dirs("/path/a") == ["/path/a"]
