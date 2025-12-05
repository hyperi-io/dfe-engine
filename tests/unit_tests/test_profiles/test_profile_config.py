import pytest
import yaml
from unittest.mock import patch, mock_open
from dfecli.dfe_config.config_loader import ConfigurationError, DFEConfigLoader
from pathlib import Path
from click.testing import CliRunner
from dfecli.main import init_target, list_targets
import logging
from typing import Any

MOCK_CONFIG = """
default_target: target_1
targets:
  target_1:
    ch_host: 'localhost'
    ch_port: 8123
    ch_username: 'user1'
    ch_password: 'password1'
    ip_config_bucket_name: 'dev_config_bucket_afterburner'
    ip_config_bucket_region: 'ap-southeast-2'
    ip_config_standard_enrichment_path: 'vector_templates/standard_enrichment_files'
    ip_config_receiver_path: 'vector_templates/vector_receiver'
    ip_config_geo_ip_path: 'vector_templates/geoip'
    ip_templates_path: 'vector_templates'
    hunt_config_path: 'dfecli/stable/hunt'
    hunt_rules_path: 'dfecli/stable/rules'
  target_2:
    ch_host: 'localhost'
    ch_port: 8123
    ch_username: 'user1'
    ch_password: 'password1'
    ip_config_bucket_name: 'dev_config_bucket_afterburner'
    ip_config_bucket_region: 'ap-southeast-2'
    ip_config_standard_enrichment_path: 'vector_templates/standard_enrichment_files'
    ip_config_receiver_path: 'vector_templates/vector_receiver'
    ip_config_geo_ip_path: 'vector_templates/geoip'
    ip_templates_path: 'vector_templates'
    hunt_config_path: 'dfecli/stable/hunts'
    hunt_rules_path: 'dfecli/stable/rules'
"""


def test_read_target_config_file_exists(
    mock_env_variables: Any, mock_config_file: Any, monkeypatch: Any
) -> None:
    monkeypatch.setattr(
        DFEConfigLoader, "get_target_config_file", lambda: Path("mock_config_file")
    )
    monkeypatch.setattr(Path, "exists", lambda x: False)
    monkeypatch.setattr(Path, "is_file", lambda x: False)
    with pytest.raises(ConfigurationError) as exc_info:
        DFEConfigLoader.read_target_config(target_name="target_1")
        # assert exc_info.value.code == 1


def test_check_dfe_folder_exists(monkeypatch: Any) -> None:
    def mock_is_dir(x):
        return True

    monkeypatch.setattr(Path, "is_dir", mock_is_dir)
    expected_dir = Path.home() / ".dfe"
    result = DFEConfigLoader.get_config_dir()
    assert expected_dir == result


def test_check_targets_file_exists(monkeypatch: Any) -> None:
    def mock_is_dir(x):
        return True

    def mock_is_file(x):
        return True

    monkeypatch.setattr(Path, "is_dir", mock_is_dir)
    monkeypatch.setattr(Path, "is_file", mock_is_file)
    expected_file = Path.home() / ".dfe" / "dfe_targets.yaml"
    result = DFEConfigLoader.get_target_config_file()
    assert expected_file == result


def test_read_target_config_file_not_exists(monkeypatch: Any) -> None:
    monkeypatch.setattr(Path, "exists", lambda x: False)
    monkeypatch.setattr(
        DFEConfigLoader, "get_target_config_file", lambda: Path("mock_config_file")
    )
    with pytest.raises(ConfigurationError) as exc_info:
        DFEConfigLoader.read_target_config("target_1")


def test_init_config_existing_profile(monkeypatch: Any) -> None:
    runner = CliRunner()

    monkeypatch.setattr(
        DFEConfigLoader,
        "read_target_config",
        lambda x: yaml.safe_load(MOCK_CONFIG)["targets"]["target_1"],
    )
    result = runner.invoke(init_target, ["--target", "target_1"])
    assert (
        "No configuration found for profile 'target_1'.\nTarget doesn't exist. Do you want to add a new profile?"
        in result.output
    )

    monkeypatch.setattr(
        DFEConfigLoader,
        "read_target_config",
        lambda x: yaml.safe_load(MOCK_CONFIG)["targets"]["target_2"],
    )
    result = runner.invoke(init_target, ["--target", "target_2"])
    assert (
        "No configuration found for profile 'target_2'.\nTarget doesn't exist. Do you want to add a new profile?"
        in result.output
    )


def test_init_config_new_profile(monkeypatch: Any) -> None:
    runner = CliRunner()
    monkeypatch.setattr(
        DFEConfigLoader,
        "read_target_config",
        lambda x: exec('raise ConfigurationError("Configuration file not found.")'),
    )
    mock_file = mock_open()
    monkeypatch.setattr("builtins.open", mock_file)

    responses = ["y", "localhost", "9000", "default", "password", "target_1"]
    result = runner.invoke(
        init_target, ["--target", "target_1"], input="\n".join(responses)
    )

    assert "No configuration found for profile 'target_1'" in result.output
    assert (
        "Target doesn't exist. Do you want to add a new profile? [y/N]" in result.output
    )
    assert mock_file.called

    result = runner.invoke(
        init_target, ["--target", "target_2"], input="\n".join(responses)
    )
    assert "No configuration found for profile 'target_2'" in result.output
    assert (
        "Target doesn't exist. Do you want to add a new profile? [y/N]" in result.output
    )


def test_list_profiles(mock_config_file: Any, mock_path_exists: Any) -> None:
    runner = CliRunner()
    result = runner.invoke(list_targets, [])

    assert "target_1" in result.output
    assert "target_2" in result.output
    assert "Available targets:" in result.output
    assert result.exit_code == 0


def test_print_default_profile_from_file(caplog: Any, test_logger: Any) -> None:
    with patch("builtins.open", mock_open(read_data=MOCK_CONFIG)):
        with patch("pathlib.Path.exists", return_value=True):
            with caplog.at_level(logging.INFO):
                DFEConfigLoader.print_default_target(test_logger)

    expected_log_message = "Default [target_1] Target Configuration"
    assert expected_log_message in caplog.text
