import pytest
import logging
from dfe_engine.config.config_loader import DFEConfigLoader
from dfe_engine.schema.schema_builder import SchemaBuilder
from dfe_engine.schema.schema_executor import SchemaExecutor
from pathlib import Path

logger = logging.getLogger(__name__)

# Mark test_execute_* as integration tests since they need ClickHouse


def test_build_schema(dfe_config_fixtures):
    """Test that SchemaBuilder correctly builds schema files."""
    config_data = dfe_config_fixtures
    schema_output_path = Path(config_data["global_settings"]["schema_output_path"])
    builder = SchemaBuilder(
        derived_schema_path=Path(config_data["global_settings"]["derived_schema_paths"]),
        schema_filter_list="logs_alerts",
        use_replicated_merge_tree=config_data["build_schemas"]["use_replicated_merge_tree"],
        use_shared_merge_tree=config_data["build_schemas"]["use_shared_merge_tree"],
        no_cluster_declarations_needed=config_data["build_schemas"][
            "no_cluster_declarations_needed"
        ],
        logger=logger,
        config=config_data,
    )

    builder.build()
    # Note: caplog doesn't capture hs-lib logger (structlog) output
    # Verify actual output files instead
    assert builder.schema_filter_list == "logs_alerts", (
        "Schema filter list does not match expected value."
    )

    assert (schema_output_path / "logs_alerts" / "logs_alerts.sql").exists(), (
        "logs_alerts schema file was not created."
    )
    assert (schema_output_path / "logs_alerts" / "logs_alerts.sql").stat().st_size > 0, (
        "logs_alerts schema file is empty."
    )


@pytest.mark.integration
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


def test_build_schema_backward_compatibility(dfe_config_fixtures_backward_compatibility):
    """Test backward compatibility of schema building with older config versions."""
    config_data = dfe_config_fixtures_backward_compatibility
    schema_output_path = Path(config_data["global_settings"]["schema_output_path"])
    builder = SchemaBuilder(
        derived_schema_path=Path(config_data["global_settings"]["derived_schema_paths"]),
        schema_filter_list="logs_alerts",
        use_replicated_merge_tree=config_data["build_schemas"]["use_replicated_merge_tree"],
        use_shared_merge_tree=config_data["build_schemas"]["use_shared_merge_tree"],
        no_cluster_declarations_needed=config_data["build_schemas"][
            "no_cluster_declarations_needed"
        ],
        logger=logger,
        config=config_data,
    )

    builder.build()
    # Note: caplog doesn't capture hs-lib logger (structlog) output
    # Verify actual output files instead
    assert (schema_output_path / "logs_alerts" / "logs_alerts.sql").exists(), (
        "logs_alerts schema file was not created."
    )
    assert (schema_output_path / "logs_alerts" / "logs_alerts.sql").stat().st_size > 0, (
        "logs_alerts schema file is empty."
    )
    assert builder.schema_filter_list == "logs_alerts", (
        "Schema filter list does not match expected value."
    )


@pytest.mark.integration
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
