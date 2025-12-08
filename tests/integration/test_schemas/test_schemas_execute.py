import pytest
from dfe_engine.config.config_loader import DFEConfigLoader
from dfe_engine.schema.schema_executor import SchemaExecutor
from pathlib import Path

# These tests require a running ClickHouse instance
pytestmark = pytest.mark.integration


def test_execute_schema(dfe_config_fixtures, caplog):
    config_data = dfe_config_fixtures
    Path(config_data["global_settings"]["schema_output_path"])

    target_config_data = DFEConfigLoader.read_target_config(
        target_name=config_data["global_settings"]["default_target"],
        targets_file_path=config_data["global_settings"]["target_path"],
    )
    organisations = config_data.get("organisations", [])
    executor = SchemaExecutor(
        dfe_output_directory=Path(config_data["global_settings"]["schema_output_path"]),
        organisations=organisations,
        do_add_roles=config_data["apply_schemas"]["do_add_roles"],
        use_json_feature=config_data["global_settings"]["use_json_feature"],
        target_config_data=target_config_data,
        schema_filter_list="logs_alerts",
    )

    executor.run_sql_scripts()


def test_execute_schema_backward_compatibility(dfe_config_fixtures_backward_compatibility, caplog):
    config_data = dfe_config_fixtures_backward_compatibility
    Path(config_data["global_settings"]["schema_output_path"])

    target_config_data = DFEConfigLoader.read_target_config(
        target_name=config_data["global_settings"]["default_target"],
        targets_file_path=config_data["global_settings"]["target_path"],
    )
    organisations = config_data.get("organisations", [])
    executor = SchemaExecutor(
        dfe_output_directory=Path(config_data["global_settings"]["schema_output_path"]),
        organisations=organisations,
        do_add_roles=config_data["apply_schemas"]["do_add_roles"],
        use_json_feature=config_data["global_settings"]["use_json_feature"],
        target_config_data=target_config_data,
        schema_filter_list="logs_alerts",
    )

    executor.run_sql_scripts()
    assert "ERROR | Error processing ClickHouse schemas" not in caplog.text, (
        "Error message found in logs, schema processing failed"
    )
