import logging
from dfe_engine.schema.schema_builder import SchemaBuilder
from pathlib import Path

logger = logging.getLogger(__name__)


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
    # Note: caplog doesn't capture hs-pylib logger (structlog) output
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
    # Note: caplog doesn't capture hs-pylib logger (structlog) output
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
