import pytest
from unittest.mock import patch

# Skip all tests in this module - CLI tests belong to dfe-cli, not dfe-engine library
pytestmark = pytest.mark.skip(reason="CLI tests belong to dfe-cli package, not dfe-engine library")

# Placeholder imports for skipped tests - actual implementations are in dfe-cli
CliRunner = None
cli = None


@pytest.fixture
def runner():
    """Provides a Click runner to invoke CLI commands."""
    return CliRunner()


def test_run_hunt(
    runner, capsys: pytest.CaptureFixture, dfe_config_fixtures, setup_paths, dfe_package
):
    with capsys.disabled():
        with patch(
            "dfe_engine.hunts.hunts.hunts_controller.HuntController.process_hunt"
        ) as mock_process_hunt:
            result = runner.invoke(
                cli,
                [
                    "run-hunt",
                    "--dfe_package_file_path",
                    dfe_package,
                    "--hunt_dir",
                    dfe_config_fixtures["hunt_scheduler"]["hunt_config_path"],
                    "--hunt_rule_repo_dir",
                    dfe_config_fixtures["hunt_scheduler"]["hunt_rules_path"],
                    "--hunt_timeout",
                    60,
                    "--checkpoint_timestamp_field",
                    "timestamp",
                    "--checkpoint_destination",
                    dfe_config_fixtures["hunt_scheduler"]["checkpoint_destination"],
                    "--log_path",
                    setup_paths[0],
                    "--hunt_log_path",
                    setup_paths[1],
                    "--hunt_num_threads",
                    dfe_config_fixtures["hunt_scheduler"]["num_threads"],
                    "--target",
                    dfe_config_fixtures["global_settings"]["default_target"],
                    "--target_file_path",
                    dfe_config_fixtures["global_settings"]["target_path"],
                    "--test_mode",
                ],
                catch_exceptions=True,
            )

            mock_process_hunt.assert_called_once()
            assert mock_process_hunt.call_args[1]["test_mode"]

    assert result.exit_code == 0, f"CLI command failed with error: {result.output}"


def test_run_hunt_with_comma_separated_paths(
    runner,
    capsys: pytest.CaptureFixture,
    dfe_config_fixtures,
    setup_paths,
    dfe_package,
    tmp_path,
):
    """Test the run-hunt command with comma-separated hunt and rule directories."""
    hunt_dirs = []
    rule_dirs = []

    for i in range(3):
        hunt_dir = tmp_path / f"test_hunt_dir_{i}"
        rule_dir = tmp_path / f"test_rule_dir_{i}"
        hunt_dir.mkdir(parents=True, exist_ok=True)
        rule_dir.mkdir(parents=True, exist_ok=True)

        (hunt_dir / f"test_hunt_{i}.yaml").write_text(f"""
name: Test Hunt {i}
cron: ["* * * * *"]
log_buffer: 60
global_target_table_name: logs_alerts
global_source_table_name: logs_nxlog_windows
customers: ["detectionlab"]
rules:
  - rule_name: test_rule_{i}
    initial_checkpoint_lookback_minutes: 60
customer_filters:
  detectionlab:
    rules:
      - name: test_rule_{i}.jinja2
        filter_clause: ""
        """)

        (rule_dir / f"test_rule_{i}.jinja2").write_text(f"""
INSERT INTO {{{{ org_id }}}}.{{{{ target_table_name }}}}
SELECT 'Test Rule {i}', NOW(), '{{{{ org_id }}}}', '{{{{ source_table_name }}}}' 
FROM {{{{ org_id }}}}.{{{{ source_table_name }}}}
WHERE {{{{timestamp_condition}}}} AND event_id = '{i}'
        """)

        hunt_dirs.append(str(hunt_dir))
        rule_dirs.append(str(rule_dir))

    hunt_dirs_str = ",".join(hunt_dirs)
    rule_dirs_str = ",".join(rule_dirs)

    with capsys.disabled():
        with patch(
            "dfe_engine.hunts.hunts.hunts_controller.HuntController.process_hunt"
        ) as mock_process_hunt:
            result = runner.invoke(
                cli,
                [
                    "run-hunt",
                    "--dfe_package_file_path",
                    dfe_package,
                    "--hunt_dir",
                    hunt_dirs_str,
                    "--hunt_rule_repo_dir",
                    rule_dirs_str,
                    "--hunt_timeout",
                    60,
                    "--checkpoint_timestamp_field",
                    "timestamp",
                    "--checkpoint_destination",
                    dfe_config_fixtures["hunt_scheduler"]["checkpoint_destination"],
                    "--log_path",
                    setup_paths[0],
                    "--hunt_log_path",
                    setup_paths[1],
                    "--hunt_num_threads",
                    dfe_config_fixtures["hunt_scheduler"]["num_threads"],
                    "--target",
                    dfe_config_fixtures["global_settings"]["default_target"],
                    "--target_file_path",
                    dfe_config_fixtures["global_settings"]["target_path"],
                    "--test_mode",
                ],
                catch_exceptions=True,
            )

            mock_process_hunt.assert_called_once()
            assert mock_process_hunt.call_args[1]["test_mode"]
            assert mock_process_hunt.call_args[1]["arg_hunt_dir"] == hunt_dirs_str
            assert mock_process_hunt.call_args[1]["arg_hunt_rule_repo_dir"] == rule_dirs_str

    assert result.exit_code == 0, f"CLI command failed with error: {result.output}"
