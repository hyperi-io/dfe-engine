import logging
import shutil
from dfecli.dfe_config.config_loader import DFEConfigLoader
from dfecli.dfe_schemabuilder.schema_builder import SchemaBuilder
from dfecli.dfe_schemabuilder.schema_executor import SchemaExecutor
from pathlib import Path

logger = logging.getLogger(__name__)


def test_build_schema(dfe_config_fixtures, caplog):
    config_data = dfe_config_fixtures
    schema_output_path = Path(config_data["global_settings"]["schema_output_path"])
    builder = SchemaBuilder(
        derived_schema_path=Path(
            config_data["global_settings"]["derived_schema_paths"]
        ),
        schema_filter_list="logs_alerts",
        use_replicated_merge_tree=config_data["build_schemas"][
            "use_replicated_merge_tree"
        ],
        use_shared_merge_tree=config_data["build_schemas"]["use_shared_merge_tree"],
        no_cluster_declarations_needed=config_data["build_schemas"][
            "no_cluster_declarations_needed"
        ],
        logger=logger,
        config=config_data,
    )

    builder.build()
    assert "ClickHouse Schema Built logs_alerts" in caplog.text, "Expected log message for schema build was not found."
    assert builder.schema_filter_list == 'logs_alerts', "Schema filter list does not match expected value."
    
    assert (schema_output_path/"logs_alerts"/"logs_alerts.sql").exists(), "logs_alerts schema file was not created."
    assert (schema_output_path/"logs_alerts"/"logs_alerts.sql").stat().st_size > 0, "logs_alerts schema file is empty."


def test_build_schema_without_opensearch_flag(dfe_config_fixtures):
    config_data = dfe_config_fixtures
    schema_output_path = Path(config_data["global_settings"]["schema_output_path"])
    opensearch_folder = schema_output_path / "opensearch"

    if opensearch_folder.exists() and opensearch_folder.is_dir():
        shutil.rmtree(opensearch_folder)

    builder = SchemaBuilder(
        derived_schema_path=Path(
            config_data["global_settings"]["derived_schema_paths"]
        ),
        schema_filter_list="logs_beats_winlogbeat",
        use_replicated_merge_tree=config_data["build_schemas"][
            "use_replicated_merge_tree"
        ],
        use_shared_merge_tree=config_data["build_schemas"]["use_shared_merge_tree"],
        no_cluster_declarations_needed=config_data["build_schemas"][
            "no_cluster_declarations_needed"
        ],
        logger=logger,
        config=config_data,
        opensearch_flag=False,
    )
    builder.build()
    schema_file_path = (
        opensearch_folder
        / "logs_beats_winlogbeat"
        / "logs_beats_winlogbeat_opensearch_template.json"
    )
    assert not schema_file_path.exists(), (
        f"Schema file {schema_file_path} should not have been created when OpenSearch is disabled."
    )


def test_build_schema_with_opensearch_flag(dfe_config_fixtures, caplog):
    config_data = dfe_config_fixtures
    schema_output_path = Path(config_data["global_settings"]["schema_output_path"])
    opensearch_folder = schema_output_path / "opensearch"

    if opensearch_folder.exists() and opensearch_folder.is_dir():
        shutil.rmtree(opensearch_folder)

    builder = SchemaBuilder(
        derived_schema_path=Path(
            config_data["global_settings"]["derived_schema_paths"]
        ),
        schema_filter_list="logs_beats_winlogbeat",
        use_replicated_merge_tree=config_data["build_schemas"][
            "use_replicated_merge_tree"
        ],
        use_shared_merge_tree=config_data["build_schemas"]["use_shared_merge_tree"],
        no_cluster_declarations_needed=config_data["build_schemas"][
            "no_cluster_declarations_needed"
        ],
        logger=logger,
        config=config_data,
        opensearch_flag=True,
    )

    builder.build()
    schema_file_path = (
        opensearch_folder
        / "logs_beats_winlogbeat"
        / "logs_beats_winlogbeat_opensearch_template.json"
    )
    assert schema_file_path.exists(), "OpenSearch index template was not created."
    assert schema_file_path.stat().st_size > 0, "OpenSearch index template is empty."


def test_execute_schema(dfe_config_fixtures, caplog):
    config_data = dfe_config_fixtures
    schema_output_path = Path(config_data["global_settings"]["schema_output_path"])

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
        logger=logger,
        target_config_data=target_config_data,
        schema_filter_list="logs_alerts",
    )

    executor.run_sql_scripts()

def test_build_schema_backward_compatibility(
    dfe_config_fixtures_backward_compatibility, caplog
):
    config_data = dfe_config_fixtures_backward_compatibility
    schema_output_path = Path(config_data["global_settings"]["schema_output_path"])
    builder = SchemaBuilder(
        derived_schema_path=Path(
            config_data["global_settings"]["derived_schema_paths"]
        ),
        schema_filter_list="logs_alerts",
        use_replicated_merge_tree=config_data["build_schemas"][
            "use_replicated_merge_tree"
        ],
        use_shared_merge_tree=config_data["build_schemas"]["use_shared_merge_tree"],
        no_cluster_declarations_needed=config_data["build_schemas"][
            "no_cluster_declarations_needed"
        ],
        logger=logger,
        config=config_data,
    )

    builder.build()
    assert (schema_output_path/"logs_alerts"/"logs_alerts.sql").exists(), "logs_alerts schema file was not created."
    assert (schema_output_path/"logs_alerts"/"logs_alerts.sql").stat().st_size > 0, "logs_alerts schema file is empty."
    assert "ClickHouse Schema Built logs_alerts" in caplog.text, "Expected log message for schema build was not found."
    assert builder.schema_filter_list == 'logs_alerts', "Schema filter list does not match expected value."
    assert "ERROR | Error processing ClickHouse schemas" not in caplog.text, "Error message found in logs, schema processing failed"


def test_build_schema_without_opensearch_flag_backward_compatibility(
    dfe_config_fixtures_backward_compatibility, caplog
):
    config_data = dfe_config_fixtures_backward_compatibility
    schema_output_path = Path(config_data["global_settings"]["schema_output_path"])
    opensearch_folder = schema_output_path / "opensearch"

    if opensearch_folder.exists() and opensearch_folder.is_dir():
        shutil.rmtree(opensearch_folder)

    builder = SchemaBuilder(
        derived_schema_path=Path(
            config_data["global_settings"]["derived_schema_paths"]
        ),
        schema_filter_list="logs_beats_winlogbeat",
        use_replicated_merge_tree=config_data["build_schemas"][
            "use_replicated_merge_tree"
        ],
        use_shared_merge_tree=config_data["build_schemas"]["use_shared_merge_tree"],
        no_cluster_declarations_needed=config_data["build_schemas"][
            "no_cluster_declarations_needed"
        ],
        logger=logger,
        config=config_data,
        opensearch_flag=False,
    )
    builder.build()
    schema_file_path = (
        opensearch_folder
        / "logs_beats_winlogbeat"
        / "logs_beats_winlogbeat_opensearch_template.json"
    )
    assert not schema_file_path.exists(), (
        f"Schema file {schema_file_path} should not have been created when OpenSearch is disabled."
    )
    assert "ERROR | Error processing ClickHouse schemas" not in caplog.text, (
        "Error message found in logs, schema processing failed"
    )


def test_build_schema_with_opensearch_flag_backward_compatibility(
    dfe_config_fixtures_backward_compatibility, caplog
):
    config_data = dfe_config_fixtures_backward_compatibility
    schema_output_path = Path(config_data["global_settings"]["schema_output_path"])
    opensearch_folder = schema_output_path / "opensearch"

    if opensearch_folder.exists() and opensearch_folder.is_dir():
        shutil.rmtree(opensearch_folder)

    builder = SchemaBuilder(
        derived_schema_path=Path(
            config_data["global_settings"]["derived_schema_paths"]
        ),
        schema_filter_list="logs_beats_winlogbeat",
        use_replicated_merge_tree=config_data["build_schemas"][
            "use_replicated_merge_tree"
        ],
        use_shared_merge_tree=config_data["build_schemas"]["use_shared_merge_tree"],
        no_cluster_declarations_needed=config_data["build_schemas"][
            "no_cluster_declarations_needed"
        ],
        logger=logger,
        config=config_data,
        opensearch_flag=True,
    )

    builder.build()
    schema_file_path = (
        opensearch_folder
        / "logs_beats_winlogbeat"
        / "logs_beats_winlogbeat_opensearch_template.json"
    )
    assert schema_file_path.exists(), "OpenSearch index template was not created."
    assert schema_file_path.stat().st_size > 0, "OpenSearch index template is empty."
    assert "ERROR | Error processing ClickHouse schemas" not in caplog.text, (
        "Error message found in logs, schema processing failed"
    )


def test_execute_schema_backward_compatibility(
    dfe_config_fixtures_backward_compatibility, caplog
):
    config_data = dfe_config_fixtures_backward_compatibility
    schema_output_path = Path(config_data["global_settings"]["schema_output_path"])

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
        logger=logger,
        target_config_data=target_config_data,
        schema_filter_list="logs_alerts",
    )

    executor.run_sql_scripts()
    assert "ERROR | Error processing ClickHouse schemas" not in caplog.text, "Error message found in logs, schema processing failed"
