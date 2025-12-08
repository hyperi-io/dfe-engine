import pytest
import logging
from pathlib import Path
from dfe_engine.schema.schema_controller import SchemaController
from dfe_engine.config.config_loader import DFEConfigLoader
from dfe_engine.clickhouse.clickhouse_manager import ClickHouseManager
from datetime import datetime, timedelta
from dfe_engine.schema.schema_update import SchemaModifier

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
handler = logging.StreamHandler()
handler.setLevel(logging.DEBUG)
logger.addHandler(handler)


@pytest.fixture(scope="module")
def ch_client(dfe_config_fixtures):
    config = DFEConfigLoader.read_clickhouse_config(
        target_name=dfe_config_fixtures["global_settings"]["default_target"],
        targets_file_path=dfe_config_fixtures["global_settings"]["target_path"],
    )
    print(f"*** using config {config}")

    ch_client = ClickHouseManager.get_instance(target_config_data=config).get_clickhouse_client()

    yield ch_client


def validate_schema_exists(ch_client, org_id, schema_name):
    query = f"SHOW TABLES FROM {org_id} LIKE '{schema_name}'"
    result = ch_client.execute(query)
    assert len(result) > 0, f"Schema '{schema_name}' does not exist in the database."


def validate_schema_fields(ch_client, org_id, schema_name, expected_fields):
    query = f"DESCRIBE TABLE {org_id}.{schema_name}"
    result = ch_client.execute(query)
    result_fields = [field[0] for field in result]
    assert all(field in result_fields for field in expected_fields), (
        f"Expected fields {expected_fields} not found in schema '{schema_name}'. Found: {result_fields}"
    )


def test_build_schema(dfe_config_fixtures, dfe_package, setup_paths):
    """
    Test the building of schemas.
    """
    try:
        SchemaController.build_schemas(
            args_dfe_package_file_path=dfe_package,
            args_schema_directory=Path(
                dfe_config_fixtures["global_settings"]["derived_schema_paths"]
            ),
            args_schema_filter_list="logs_alerts",
            args_no_cluster_declarations_needed=dfe_config_fixtures["build_schemas"][
                "no_cluster_declarations_needed"
            ],
            args_use_replicated_merge_tree=dfe_config_fixtures["build_schemas"][
                "use_replicated_merge_tree"
            ],
            args_use_json_feature=dfe_config_fixtures["global_settings"][
                "use_json_feature"
            ],
            args_use_subsampling_feature=dfe_config_fixtures["global_settings"].get(
                "use_subsampling_feature", False
            ),
            args_use_shared_merge_tree=dfe_config_fixtures["build_schemas"][
                "use_shared_merge_tree"
            ],
            args_log_path=setup_paths,
            args_target=dfe_config_fixtures["global_settings"]["default_target"],
            args_target_file_path=dfe_config_fixtures["global_settings"]["target_path"],
        )
        expected_schema_path = (
            Path(dfe_config_fixtures["global_settings"]["schema_output_path"]).resolve()
            / "logs_alerts"
            / "logs_alerts.sql"
        )
        assert expected_schema_path.is_file(), (
            f"Schema file not found: {expected_schema_path}"
        )

    except Exception as e:
        pytest.fail(f"Build schema failed: {e}")


def test_apply_schemas(
    ch_client, dfe_config_fixtures, dfe_package, setup_paths, ensure_schema_db
):
    """
    Test the application of schemas.
    """

    def apply_schema_operation():
        SchemaController.build_schemas(
            args_dfe_package_file_path=dfe_package,
            args_schema_directory=Path(
                dfe_config_fixtures["global_settings"]["derived_schema_paths"]
            ),
            args_schema_filter_list="logs_alerts",
            args_no_cluster_declarations_needed=dfe_config_fixtures["build_schemas"][
                "no_cluster_declarations_needed"
            ],
            args_use_replicated_merge_tree=dfe_config_fixtures["build_schemas"][
                "use_replicated_merge_tree"
            ],
            args_use_json_feature=dfe_config_fixtures["global_settings"][
                "use_json_feature"
            ],
            args_use_subsampling_feature=dfe_config_fixtures["global_settings"].get(
                "use_subsampling_feature", False
            ),
            args_use_shared_merge_tree=dfe_config_fixtures["build_schemas"][
                "use_shared_merge_tree"
            ],
            args_log_path=setup_paths,
            args_target=dfe_config_fixtures["global_settings"]["default_target"],
            args_target_file_path=dfe_config_fixtures["global_settings"]["target_path"],
        )
        
        SchemaController.apply_schemas(
            args_dfe_package_file_path=dfe_package,
            args_schema_directory=Path(
                dfe_config_fixtures["global_settings"]["derived_schema_paths"]
            ),
            args_do_add_roles=dfe_config_fixtures["apply_schemas"]["do_add_roles"],
            args_use_json_feature=dfe_config_fixtures["global_settings"][
                "use_json_feature"
            ],
            args_log_path=setup_paths,
            args_target=dfe_config_fixtures["global_settings"]["default_target"],
            args_target_file_path=dfe_config_fixtures["global_settings"]["target_path"],
            args_schema_filter_list="logs_alerts",
        )
        validate_schema_exists(ch_client, "org321", "logs_alerts")
        validate_schema_fields(
            ch_client, "org321", "logs_alerts", ["timestamp", "timestamp_load"]
        )

    try:
        ensure_schema_db("org321", apply_schema_operation)
    except Exception as e:
        pytest.fail(str(e))


def drop_schema_if_exists(ch_client, org_name, schema_name):
    """
    Drop a schema if it exists in the ClickHouse database.
    """
    query = f"DROP TABLE IF EXISTS {org_name}.{schema_name}"
    ch_client.execute(query)


def test_apply_schemas_not_exists(
    ch_client, dfe_config_fixtures, dfe_package, setup_paths, ensure_schema_db
):
    """
    Test the application of schemas, including dropping and re-adding a schema.
    """
    schema_name = "logs_alerts"
    org_name = "org321"

    def apply_schema_operation():
        drop_schema_if_exists(ch_client, org_name, schema_name)
        
        SchemaController.build_schemas(
            args_dfe_package_file_path=dfe_package,
            args_schema_directory=Path(
                dfe_config_fixtures["global_settings"]["derived_schema_paths"]
            ),
            args_schema_filter_list=schema_name,
            args_no_cluster_declarations_needed=dfe_config_fixtures["build_schemas"][
                "no_cluster_declarations_needed"
            ],
            args_use_replicated_merge_tree=dfe_config_fixtures["build_schemas"][
                "use_replicated_merge_tree"
            ],
            args_use_json_feature=dfe_config_fixtures["global_settings"][
                "use_json_feature"
            ],
            args_use_subsampling_feature=dfe_config_fixtures["global_settings"].get(
                "use_subsampling_feature", False
            ),
            args_use_shared_merge_tree=dfe_config_fixtures["build_schemas"][
                "use_shared_merge_tree"
            ],
            args_log_path=setup_paths,
            args_target=dfe_config_fixtures["global_settings"]["default_target"],
            args_target_file_path=dfe_config_fixtures["global_settings"]["target_path"],
        )
        
        SchemaController.apply_schemas(
            args_dfe_package_file_path=dfe_package,
            args_schema_directory=Path(
                dfe_config_fixtures["global_settings"]["derived_schema_paths"]
            ),
            args_do_add_roles=dfe_config_fixtures["apply_schemas"]["do_add_roles"],
            args_use_json_feature=dfe_config_fixtures["global_settings"][
                "use_json_feature"
            ],
            args_log_path=setup_paths,
            args_target=dfe_config_fixtures["global_settings"]["default_target"],
            args_target_file_path=dfe_config_fixtures["global_settings"]["target_path"],
            args_schema_filter_list=schema_name,
        )
        validate_schema_exists(ch_client, org_name, schema_name)
        validate_schema_fields(
            ch_client, org_name, schema_name, ["timestamp", "timestamp_load"]
        )

    try:
        ensure_schema_db(org_name, apply_schema_operation)
    except Exception as e:
        pytest.fail(str(e))


def test_plan_schemas(dfe_config_fixtures, dfe_package, setup_paths):
    """
    Test the application of schemas.
    """
    try:
        SchemaController.plan_schemas(
            args_dfe_package_file_path=dfe_package,
            args_log_path=setup_paths,
            args_target=dfe_config_fixtures["global_settings"]["default_target"],
            args_target_file_path=dfe_config_fixtures["global_settings"]["target_path"],
            args_schema_filter_list="logs_alerts",
        )
    except Exception as e:
        pytest.fail(f"Schema Plan failed: {e}")


@pytest.mark.parametrize("org_filter", [("org321,org111111"), ("org321")])
def test_plan_schemas_with_org_spec(
    dfe_config_fixtures, dfe_package, setup_paths, org_filter
):
    """
    Test the planning of schemas with organisation specification.
    """
    try:
        SchemaController.plan_schemas(
            args_dfe_package_file_path=dfe_package,
            args_log_path=setup_paths,
            args_target=dfe_config_fixtures["global_settings"]["default_target"],
            args_target_file_path=dfe_config_fixtures["global_settings"]["target_path"],
            args_org_filter_list=org_filter,
        )
    except Exception as e:
        pytest.fail(f"Schema plan with organisation specification failed: {e}")


@pytest.mark.parametrize(
    "schema_filter",
    [
        ("logs_alerts, nx_log_windows_sub_ce"),
        ("logs_alerts,nx_log_windows_sub_ce"),
        ("logs_alerts"),
    ],
)
def test_plan_schemas_with_schema_spec(
    dfe_config_fixtures, dfe_package, setup_paths, schema_filter
):
    """
    Test the planning of schemas with schema specification.
    """
    try:
        SchemaController.plan_schemas(
            args_dfe_package_file_path=dfe_package,
            args_log_path=setup_paths,
            args_target=dfe_config_fixtures["global_settings"]["default_target"],
            args_target_file_path=dfe_config_fixtures["global_settings"]["target_path"],
            args_schema_filter_list=schema_filter,
        )
    except Exception as e:
        pytest.fail(f"Schema plan with schema specification failed: {e}")


@pytest.mark.parametrize("schema_filter", [("logs_alerts")])
def test_update_schemas(
    ch_client,
    dfe_config_fixtures,
    dfe_package,
    setup_paths,
    schema_filter,
    ensure_schema_db,
):
    """
    Test the updating of schemas.
    """

    def update_schema_operation():
        SchemaController.build_schemas(
            args_dfe_package_file_path=dfe_package,
            args_schema_directory=Path(
                dfe_config_fixtures["global_settings"]["derived_schema_paths"]
            ),
            args_schema_filter_list=schema_filter,
            args_no_cluster_declarations_needed=dfe_config_fixtures["build_schemas"][
                "no_cluster_declarations_needed"
            ],
            args_use_replicated_merge_tree=dfe_config_fixtures["build_schemas"][
                "use_replicated_merge_tree"
            ],
            args_use_json_feature=dfe_config_fixtures["global_settings"][
                "use_json_feature"
            ],
            args_use_subsampling_feature=dfe_config_fixtures["global_settings"].get(
                "use_subsampling_feature", False
            ),
            args_use_shared_merge_tree=dfe_config_fixtures["build_schemas"][
                "use_shared_merge_tree"
            ],
            args_log_path=setup_paths,
            args_target=dfe_config_fixtures["global_settings"]["default_target"],
            args_target_file_path=dfe_config_fixtures["global_settings"]["target_path"],
        )
        
        SchemaController.apply_schemas(
            args_dfe_package_file_path=dfe_package,
            args_schema_directory=Path(
                dfe_config_fixtures["global_settings"]["derived_schema_paths"]
            ),
            args_do_add_roles=dfe_config_fixtures["apply_schemas"]["do_add_roles"],
            args_use_json_feature=dfe_config_fixtures["global_settings"][
                "use_json_feature"
            ],
            args_log_path=setup_paths,
            args_target=dfe_config_fixtures["global_settings"]["default_target"],
            args_target_file_path=dfe_config_fixtures["global_settings"]["target_path"],
            args_schema_filter_list=schema_filter,
        )
        
        SchemaController.modify_schemas(
            args_dfe_package_file_path=dfe_package,
            args_schema_update_flag="YES",
            args_drop_replacement_table_flag="YES",
            args_log_path=setup_paths,
            args_use_replicated_merge_tree=dfe_config_fixtures["build_schemas"][
                "use_replicated_merge_tree"
            ],
            args_use_shared_merge_tree=dfe_config_fixtures["build_schemas"][
                "use_shared_merge_tree"
            ],
            args_do_add_columns=dfe_config_fixtures["apply_schemas"]["do_add_columns"],
            args_use_json_feature=dfe_config_fixtures["global_settings"][
                "use_json_feature"
            ],
            args_use_subsampling_feature=dfe_config_fixtures["global_settings"].get(
                "use_subsampling_feature", False
            ),
            args_target=dfe_config_fixtures["global_settings"]["default_target"],
            args_target_file_path=dfe_config_fixtures["global_settings"]["target_path"],
            args_max_insert_threads=4,
            args_min_insert_block_size_rows=1048576,
            args_schema_filter_list=schema_filter,
            args_org_filter_list="org321",
        )
        validate_schema_exists(ch_client, "org321", "logs_alerts")
        validate_schema_fields(
            ch_client, "org321", "logs_alerts", ["timestamp", "timestamp_load"]
        )

    try:
        ensure_schema_db("org321", update_schema_operation)
    except Exception as e:
        pytest.fail(str(e))


@pytest.mark.parametrize("schema_filter", [("logs_alerts")])
def test_update_schemas_with_data(
    ch_client,
    dfe_config_fixtures,
    dfe_package,
    setup_paths,
    schema_filter,
    ensure_schema_db,
):
    """
    Test the updating when the schema holds data.
    """

    def update_schema_with_data_operation():
        drop_schema_if_exists(ch_client, "org321", "logs_alerts")
        
        SchemaController.build_schemas(
            args_dfe_package_file_path=dfe_package,
            args_schema_directory=Path(
                dfe_config_fixtures["global_settings"]["derived_schema_paths"]
            ),
            args_schema_filter_list=schema_filter,
            args_no_cluster_declarations_needed=dfe_config_fixtures["build_schemas"][
                "no_cluster_declarations_needed"
            ],
            args_use_replicated_merge_tree=dfe_config_fixtures["build_schemas"][
                "use_replicated_merge_tree"
            ],
            args_use_json_feature=dfe_config_fixtures["global_settings"][
                "use_json_feature"
            ],
            args_use_subsampling_feature=dfe_config_fixtures["global_settings"].get(
                "use_subsampling_feature", False
            ),
            args_use_shared_merge_tree=dfe_config_fixtures["build_schemas"][
                "use_shared_merge_tree"
            ],
            args_log_path=setup_paths,
            args_target=dfe_config_fixtures["global_settings"]["default_target"],
            args_target_file_path=dfe_config_fixtures["global_settings"]["target_path"],
        )
        
        SchemaController.apply_schemas(
            args_dfe_package_file_path=dfe_package,
            args_schema_directory=Path(
                dfe_config_fixtures["global_settings"]["derived_schema_paths"]
            ),
            args_do_add_roles=dfe_config_fixtures["apply_schemas"]["do_add_roles"],
            args_use_json_feature=dfe_config_fixtures["global_settings"][
                "use_json_feature"
            ],
            args_log_path=setup_paths,
            args_target=dfe_config_fixtures["global_settings"]["default_target"],
            args_target_file_path=dfe_config_fixtures["global_settings"]["target_path"],
            args_schema_filter_list=schema_filter,
        )

        num_of_inserts = 10
        values = []
        current_time = datetime.now()
        for i in range(num_of_inserts):
            current_time = datetime.now() + timedelta(days=365*10) - timedelta(seconds=i)  # Use future timestamps to avoid TTL deletion
            timestamp_str = current_time.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            values.append(f"('{timestamp_str}', '{timestamp_str}', 'test_hash_{i}', 'test_log_{i}', 'org321')")
        query = (
            "INSERT INTO org321.logs_alerts (timestamp, timestamp_load, event_hash, logoriginal, org_id) VALUES"
            + ", ".join(values)
        )
        ch_client.execute(query)

        SchemaController.modify_schemas(
            args_dfe_package_file_path=dfe_package,
            args_schema_update_flag="YES",
            args_drop_replacement_table_flag="YES",
            args_log_path=setup_paths,
            args_use_replicated_merge_tree=dfe_config_fixtures["build_schemas"][
                "use_replicated_merge_tree"
            ],
            args_use_shared_merge_tree=dfe_config_fixtures["build_schemas"][
                "use_shared_merge_tree"
            ],
            args_do_add_columns=dfe_config_fixtures["apply_schemas"]["do_add_columns"],
            args_use_json_feature=dfe_config_fixtures["global_settings"][
                "use_json_feature"
            ],
            args_use_subsampling_feature=dfe_config_fixtures["global_settings"].get(
                "use_subsampling_feature", False
            ),
            args_target=dfe_config_fixtures["global_settings"]["default_target"],
            args_target_file_path=dfe_config_fixtures["global_settings"]["target_path"],
            args_max_insert_threads=4,
            args_min_insert_block_size_rows=1048576,
            args_schema_filter_list=schema_filter,
            args_org_filter_list="org321",
        )

        validate_schema_exists(ch_client, "org321", "logs_alerts")
        validate_schema_fields(
            ch_client, "org321", "logs_alerts", ["timestamp", "timestamp_load"]
        )

    try:
        ensure_schema_db("org321", update_schema_with_data_operation)
    except Exception as e:
        pytest.fail(str(e))


@pytest.mark.parametrize(
    "org_filter, schema_filter",
    [("org321,org111111", "logs_alerts"), ("org321", "logs_alerts")],
)
def test_update_schemas_with_org_spec(
    ch_client,
    dfe_config_fixtures,
    dfe_package,
    setup_paths,
    org_filter,
    schema_filter,
    ensure_schema_db,
):
    """
    Test the updating of schemas with an organisation specified.
    """

    def update_schema_operation():
        SchemaController.modify_schemas(
            args_dfe_package_file_path=dfe_package,
            args_schema_update_flag="YES",
            args_drop_replacement_table_flag="YES",
            args_log_path=setup_paths,
            args_use_replicated_merge_tree=dfe_config_fixtures["build_schemas"][
                "use_replicated_merge_tree"
            ],
            args_use_shared_merge_tree=dfe_config_fixtures["build_schemas"][
                "use_shared_merge_tree"
            ],
            args_do_add_columns=dfe_config_fixtures["apply_schemas"]["do_add_columns"],
            args_use_json_feature=dfe_config_fixtures["global_settings"][
                "use_json_feature"
            ],
            args_use_subsampling_feature=dfe_config_fixtures["global_settings"].get(
                "use_subsampling_feature", False
            ),
            args_target=dfe_config_fixtures["global_settings"]["default_target"],
            args_target_file_path=dfe_config_fixtures["global_settings"]["target_path"],
            args_org_filter_list=org_filter,
            args_schema_filter_list=schema_filter,
            args_max_insert_threads=4,
            args_min_insert_block_size_rows=1048576,
        )
        validate_schema_exists(ch_client, "org321", "logs_alerts")
        validate_schema_fields(
            ch_client, "org321", "logs_alerts", ["timestamp", "timestamp_load"]
        )

    try:
        ensure_schema_db("org321", update_schema_operation)
    except Exception as e:
        pytest.fail(str(e))


@pytest.mark.parametrize(
    "schema_filter", [("logs_alerts, nx_log_windows_sub_ce"), ("logs_alerts")]
)
def test_update_schemas_with_schema_spec(
    ch_client,
    dfe_config_fixtures,
    dfe_package,
    setup_paths,
    schema_filter,
    ensure_schema_db,
):
    """
    Test the updating of schemas with a schema specified.
    """

    def update_schema_operation():
        SchemaController.modify_schemas(
            args_dfe_package_file_path=dfe_package,
            args_schema_update_flag="YES",
            args_drop_replacement_table_flag="YES",
            args_log_path=setup_paths,
            args_use_replicated_merge_tree=dfe_config_fixtures["build_schemas"][
                "use_replicated_merge_tree"
            ],
            args_use_shared_merge_tree=dfe_config_fixtures["build_schemas"][
                "use_shared_merge_tree"
            ],
            args_do_add_columns=dfe_config_fixtures["apply_schemas"]["do_add_columns"],
            args_use_json_feature=dfe_config_fixtures["global_settings"][
                "use_json_feature"
            ],
            args_use_subsampling_feature=dfe_config_fixtures["global_settings"].get(
                "use_subsampling_feature", False
            ),
            args_target=dfe_config_fixtures["global_settings"]["default_target"],
            args_target_file_path=dfe_config_fixtures["global_settings"]["target_path"],
            args_schema_filter_list=schema_filter,
            args_org_filter_list="org321",
            args_max_insert_threads=4,
            args_min_insert_block_size_rows=1048576,
        )
        validate_schema_exists(ch_client, "org321", "logs_alerts")
        validate_schema_fields(
            ch_client, "org321", "logs_alerts", ["timestamp", "timestamp_load"]
        )

    try:
        ensure_schema_db("org321", update_schema_operation)
    except Exception as e:
        pytest.fail(str(e))


def test_download_meta_schemas(dfe_package, dfe_config_fixtures, setup_paths):
    """
    Test the download of meta schemas .
    """
    try:
        SchemaController.download_meta_schemas(
            args_dfe_package_file_path=dfe_package,
            args_log_path=setup_paths,
            args_output_zip=dfe_config_fixtures.get("download_meta_schemas", {}).get(
                "output_zip", "meta_schemas.zip"
            ),
        )
    except Exception as e:
        pytest.fail(f"Download meta schemas  failed: {e}")


def test_list_meta_schemas(setup_paths, dfe_package):
    """
    Test the listing of meta schemas .
    """
    try:
        SchemaController.list_meta_schemas(
            args_log_path=setup_paths, args_dfe_package_file_path=dfe_package
        )
    except Exception as e:
        pytest.fail(f"List meta schemas  failed: {e}")


def test_list_schema_fields(dfe_config_fixtures, dfe_package, setup_paths):
    """
    Test the listing of schema fields.
    """
    try:
        SchemaController.list_schema_fields(
            args_dfe_package_file_path=dfe_package,
            args_log_path=setup_paths,
            args_schema_name=dfe_config_fixtures["schemas"]["logs_alerts"]["name"],
            args_template_version=dfe_config_fixtures["schemas"]["logs_alerts"][
                "meta_schema_version"
            ],
        )
    except Exception as e:
        pytest.fail(f"List schema fields failed: {e}")


def test_boolean_type_normalization(
    ch_client, dfe_config_fixtures, dfe_package, setup_paths, ensure_schema_db
):
    """
    Test that equivalent boolean types are normalized correctly and don't trigger type mismatch errors.
    Tests the normalize_data_type functionality added to handle Bool/Boolean equivalence.
    """
    schema_name = "test_bool_types"
    org_name = "org321"

    def boolean_type_operation():
        ch_client.execute(f"""
            CREATE TABLE IF NOT EXISTS {org_name}.{schema_name} (
                id UInt32,
                flag1 Boolean,
                flag2 Nullable(Boolean),
                flag3 Bool,
                flag4 Nullable(Bool)
            ) ENGINE = MergeTree()
            ORDER BY id
        """)

        ch_client.execute(f"""
            CREATE TABLE IF NOT EXISTS {org_name}.{schema_name}_new (
                id UInt32,
                flag1 Bool,
                flag2 Nullable(Bool),
                flag3 Boolean,
                flag4 Nullable(Boolean)
            ) ENGINE = MergeTree()
            ORDER BY id
        """)

        try:
            existing_result = ch_client.execute(
                f"DESCRIBE TABLE {org_name}.{schema_name}"
            )
            new_result = ch_client.execute(
                f"DESCRIBE TABLE {org_name}.{schema_name}_new"
            )
            existing_columns = {row[0]: row[1] for row in existing_result}
            new_columns = {row[0]: row[1] for row in new_result}
            schema_modifier = SchemaModifier(
                schema_update_flag="YES",
                drop_replacement_table_flag="YES",
                use_replicated_merge_tree=False,
                use_shared_merge_tree=False,
                do_add_columns=True,
                use_json_feature=False,
                use_subsampling_feature=False,
                dfe_output_directory="",
                organisations=[],
                max_insert_threads=4,
                min_insert_block_size_rows=1048576,
                logger=logger,
            )
            schema_modifier.compare_columns_and_data_types(
                existing_columns, new_columns, org_name, schema_name
            )
        finally:
            ch_client.execute(f"DROP TABLE IF EXISTS {org_name}.{schema_name}")
            ch_client.execute(f"DROP TABLE IF EXISTS {org_name}.{schema_name}_new")

    try:
        ensure_schema_db(org_name, boolean_type_operation)
    except Exception as e:
        pytest.fail(str(e))


def test_apply_schemas_with_roles(
    ch_client, dfe_config_fixtures, dfe_package, setup_paths, ensure_schema_db
):
    """
    Test applying schemas with role creation enabled.
    """
    schema_name = "logs_alerts"
    org_name = "org321"

    def apply_schema_with_roles():
        SchemaController.build_schemas(
            args_dfe_package_file_path=dfe_package,
            args_schema_directory=Path(
                dfe_config_fixtures["global_settings"]["derived_schema_paths"]
            ),
            args_schema_filter_list=schema_name,
            args_no_cluster_declarations_needed=dfe_config_fixtures["build_schemas"][
                "no_cluster_declarations_needed"
            ],
            args_use_replicated_merge_tree=dfe_config_fixtures["build_schemas"][
                "use_replicated_merge_tree"
            ],
            args_use_json_feature=dfe_config_fixtures["global_settings"][
                "use_json_feature"
            ],
            args_use_subsampling_feature=dfe_config_fixtures["global_settings"].get(
                "use_subsampling_feature", False
            ),
            args_use_shared_merge_tree=dfe_config_fixtures["build_schemas"][
                "use_shared_merge_tree"
            ],
            args_log_path=setup_paths,
            args_target=dfe_config_fixtures["global_settings"]["default_target"],
            args_target_file_path=dfe_config_fixtures["global_settings"]["target_path"],
        )
        
        SchemaController.apply_schemas(
            args_dfe_package_file_path=dfe_package,
            args_schema_directory=Path(
                dfe_config_fixtures["global_settings"]["derived_schema_paths"]
            ),
            args_do_add_roles=True,  # Enable role creation
            args_use_json_feature=dfe_config_fixtures["global_settings"][
                "use_json_feature"
            ],
            args_log_path=setup_paths,
            args_target=dfe_config_fixtures["global_settings"]["default_target"],
            args_target_file_path=dfe_config_fixtures["global_settings"]["target_path"],
            args_schema_filter_list=schema_name,
        )

        roles_result = ch_client.execute("SHOW ROLES")
        role_names = [row[0] for row in roles_result]
        assert "read_only_role" in role_names
        assert "read_write_role" in role_names
        assert "loader_role" in role_names
        assert "detections_role" in role_names

    try:
        ensure_schema_db(org_name, apply_schema_with_roles)
    except Exception as e:
        pytest.fail(f"Apply schemas with roles failed: {e}")


def test_apply_schemas_with_schema_filtering(
    ch_client, dfe_config_fixtures, dfe_package, setup_paths, ensure_schema_db
):
    """
    Test applying schemas with schema filtering.
    """
    schema_name = "logs_alerts"
    org_name = "org321"

    def apply_schema_filtered():
        SchemaController.build_schemas(
            args_dfe_package_file_path=dfe_package,
            args_schema_directory=Path(
                dfe_config_fixtures["global_settings"]["derived_schema_paths"]
            ),
            args_schema_filter_list=schema_name,
            args_no_cluster_declarations_needed=dfe_config_fixtures["build_schemas"][
                "no_cluster_declarations_needed"
            ],
            args_use_replicated_merge_tree=dfe_config_fixtures["build_schemas"][
                "use_replicated_merge_tree"
            ],
            args_use_json_feature=dfe_config_fixtures["global_settings"][
                "use_json_feature"
            ],
            args_use_subsampling_feature=dfe_config_fixtures["global_settings"].get(
                "use_subsampling_feature", False
            ),
            args_use_shared_merge_tree=dfe_config_fixtures["build_schemas"][
                "use_shared_merge_tree"
            ],
            args_log_path=setup_paths,
            args_target=dfe_config_fixtures["global_settings"]["default_target"],
            args_target_file_path=dfe_config_fixtures["global_settings"]["target_path"],
        )
        
        SchemaController.apply_schemas(
            args_dfe_package_file_path=dfe_package,
            args_schema_directory=Path(
                dfe_config_fixtures["global_settings"]["derived_schema_paths"]
            ),
            args_do_add_roles=False,
            args_use_json_feature=dfe_config_fixtures["global_settings"][
                "use_json_feature"
            ],
            args_log_path=setup_paths,
            args_target=dfe_config_fixtures["global_settings"]["default_target"],
            args_target_file_path=dfe_config_fixtures["global_settings"]["target_path"],
            args_schema_filter_list=schema_name,
        )

        validate_schema_exists(ch_client, org_name, schema_name)

    try:
        ensure_schema_db(org_name, apply_schema_filtered)
    except Exception as e:
        pytest.fail(f"Apply schemas with filtering failed: {e}")


def test_apply_schemas_with_wildcard_filtering(
    ch_client, dfe_config_fixtures, dfe_package, setup_paths, ensure_schema_db
):
    """
    Test applying schemas with wildcard filtering.
    """
    org_name = "org321"

    def apply_schema_wildcard():
        SchemaController.build_schemas(
            args_dfe_package_file_path=dfe_package,
            args_schema_directory=Path(
                dfe_config_fixtures["global_settings"]["derived_schema_paths"]
            ),
            args_schema_filter_wildchar="logs_*",
            args_no_cluster_declarations_needed=dfe_config_fixtures["build_schemas"][
                "no_cluster_declarations_needed"
            ],
            args_use_replicated_merge_tree=dfe_config_fixtures["build_schemas"][
                "use_replicated_merge_tree"
            ],
            args_use_json_feature=dfe_config_fixtures["global_settings"][
                "use_json_feature"
            ],
            args_use_subsampling_feature=dfe_config_fixtures["global_settings"].get(
                "use_subsampling_feature", False
            ),
            args_use_shared_merge_tree=dfe_config_fixtures["build_schemas"][
                "use_shared_merge_tree"
            ],
            args_log_path=setup_paths,
            args_target=dfe_config_fixtures["global_settings"]["default_target"],
            args_target_file_path=dfe_config_fixtures["global_settings"]["target_path"],
        )
        
        SchemaController.apply_schemas(
            args_dfe_package_file_path=dfe_package,
            args_schema_directory=Path(
                dfe_config_fixtures["global_settings"]["derived_schema_paths"]
            ),
            args_do_add_roles=False,
            args_use_json_feature=dfe_config_fixtures["global_settings"][
                "use_json_feature"
            ],
            args_log_path=setup_paths,
            args_target=dfe_config_fixtures["global_settings"]["default_target"],
            args_target_file_path=dfe_config_fixtures["global_settings"]["target_path"],
            args_schema_filter_wildchar="logs_*",  # Apply all logs schemas
        )

        tables_result = ch_client.execute(f"SHOW TABLES FROM {org_name}")
        table_names = [row[0] for row in tables_result]
        logs_tables = [name for name in table_names if name.startswith("logs_")]
        assert len(logs_tables) > 0, "No logs tables were created with wildcard filtering"

    try:
        ensure_schema_db(org_name, apply_schema_wildcard)
    except Exception as e:
        pytest.fail(f"Apply schemas with wildcard filtering failed: {e}")


def test_apply_schemas_missing_output_directory(
    dfe_config_fixtures, dfe_package, setup_paths
):
    """
    Test applying schemas when output directory doesn't exist.
    This test verifies that apply_schemas handles missing output directory gracefully.
    """
    output_path = Path(dfe_config_fixtures["global_settings"]["schema_output_path"])
    if output_path.exists():
        import shutil
        shutil.rmtree(output_path)
    
    SchemaController.apply_schemas(
        args_dfe_package_file_path=dfe_package,
        args_schema_directory=Path(dfe_config_fixtures["global_settings"]["derived_schema_paths"]),
        args_do_add_roles=False,
        args_use_json_feature=False,
        args_log_path=setup_paths,
        args_target=dfe_config_fixtures["global_settings"]["default_target"],
        args_target_file_path=dfe_config_fixtures["global_settings"]["target_path"],
    )


def test_apply_schemas_invalid_target(
    dfe_config_fixtures, dfe_package, setup_paths
):
    """
    Test applying schemas with invalid target configuration.
    """
    with pytest.raises(SystemExit):  # Should exit due to invalid target
        SchemaController.apply_schemas(
            args_dfe_package_file_path=dfe_package,
            args_schema_directory=Path(
                dfe_config_fixtures["global_settings"]["derived_schema_paths"]
            ),
            args_do_add_roles=False,
            args_use_json_feature=False,
            args_log_path=setup_paths,
            args_target="invalid_target",
            args_target_file_path=dfe_config_fixtures["global_settings"]["target_path"],
        )


def test_apply_schemas_no_organizations(
    dfe_config_fixtures, dfe_package, setup_paths, tmp_path
):
    """
    Test applying schemas when no organizations are configured.
    This test verifies that apply_schemas handles the no organizations case gracefully.
    """
    temp_config = dfe_config_fixtures.copy()
    temp_config["organisations"] = []

    temp_config_file = tmp_path / "temp_config.yaml"
    import yaml
    with open(temp_config_file, 'w') as f:
        yaml.dump(temp_config, f)

    output_path = Path(dfe_config_fixtures["global_settings"]["schema_output_path"])
    output_path.mkdir(parents=True, exist_ok=True)

    SchemaController.apply_schemas(
        args_dfe_package_file_path=str(temp_config_file),
        args_schema_directory=Path(
            dfe_config_fixtures["global_settings"]["derived_schema_paths"]
        ),
        args_do_add_roles=False,
        args_use_json_feature=False,
        args_log_path=setup_paths,
        args_target=dfe_config_fixtures["global_settings"]["default_target"],
        args_target_file_path=dfe_config_fixtures["global_settings"]["target_path"],
    )


def test_apply_schemas_with_org_filtering(
    ch_client, dfe_config_fixtures, dfe_package, setup_paths
):
    """
    Test applying schemas with organization filtering.
    """
    schema_name = "logs_alerts"
    org_name = "org321"

    def apply_schema_org_filtered():
        SchemaController.apply_schemas(
            args_dfe_package_file_path=dfe_package,
            args_schema_directory=Path(
                dfe_config_fixtures["global_settings"]["derived_schema_paths"]
            ),
            args_do_add_roles=False,
            args_use_json_feature=dfe_config_fixtures["global_settings"][
                "use_json_feature"
            ],
            args_log_path=setup_paths,
            args_target=dfe_config_fixtures["global_settings"]["default_target"],
            args_target_file_path=dfe_config_fixtures["global_settings"]["target_path"],
            args_schema_filter_list=schema_name,
            args_org_filter_list=org_name,
        )

        validate_schema_exists(ch_client, org_name, schema_name)

    try:
        ch_client.execute(f"CREATE DATABASE IF NOT EXISTS {org_name}")
        apply_schema_org_filtered()
    except Exception as e:
        pytest.fail(f"Apply schemas with org filtering failed: {e}")
    finally:
        ch_client.execute(f"DROP DATABASE IF EXISTS {org_name}")


def test_apply_schemas_role_creation_failure(
    dfe_config_fixtures, dfe_package, setup_paths, mocker
):
    """
    Test applying schemas when role creation fails.
    This test verifies that apply_schemas handles role creation failures gracefully.
    """
    mock_schema_executor = mocker.patch('dfe_engine.schema.schema_controller.SchemaExecutor')
    mock_instance = mock_schema_executor.return_value
    
    mock_instance.run_create_database.return_value = None
    mock_instance.process_sql_scripts.return_value = None
    mock_instance.run_create_roles.side_effect = Exception("Role creation failed: insufficient privileges")
    mock_instance.cleanup.return_value = None
    
    SchemaController.build_schemas(
        args_dfe_package_file_path=dfe_package,
        args_schema_directory=Path(
            dfe_config_fixtures["global_settings"]["derived_schema_paths"]
        ),
        args_no_cluster_declarations_needed=dfe_config_fixtures["build_schemas"][
            "no_cluster_declarations_needed"
        ],
        args_use_replicated_merge_tree=dfe_config_fixtures["build_schemas"][
            "use_replicated_merge_tree"
        ],
        args_use_json_feature=dfe_config_fixtures["global_settings"][
            "use_json_feature"
        ],
        args_use_subsampling_feature=dfe_config_fixtures["global_settings"].get(
            "use_subsampling_feature", False
        ),
        args_use_shared_merge_tree=dfe_config_fixtures["build_schemas"][
            "use_shared_merge_tree"
        ],
        args_log_path=setup_paths,
        args_target=dfe_config_fixtures["global_settings"]["default_target"],
        args_target_file_path=dfe_config_fixtures["global_settings"]["target_path"],
        args_schema_filter_list="logs_alerts",
    )
    
    SchemaController.apply_schemas(
        args_dfe_package_file_path=dfe_package,
        args_schema_directory=Path(
            dfe_config_fixtures["global_settings"]["derived_schema_paths"]
        ),
        args_do_add_roles=True,  # This should fail but not prevent completion
        args_use_json_feature=dfe_config_fixtures["global_settings"][
            "use_json_feature"
        ],
        args_log_path=setup_paths,
        args_target=dfe_config_fixtures["global_settings"]["default_target"],
        args_target_file_path=dfe_config_fixtures["global_settings"]["target_path"],
        args_schema_filter_list="logs_alerts",
    )


def test_apply_schemas_concurrent_execution(
    ch_client, dfe_config_fixtures, dfe_package, setup_paths, ensure_schema_db
):
    """
    Test applying schemas with concurrent execution handling.
    """
    schema_name = "logs_alerts"
    org_name = "org321"

    def apply_schema_concurrent():
        SchemaController.apply_schemas(
            args_dfe_package_file_path=dfe_package,
            args_schema_directory=Path(
                dfe_config_fixtures["global_settings"]["derived_schema_paths"]
            ),
            args_do_add_roles=False,
            args_use_json_feature=dfe_config_fixtures["global_settings"][
                "use_json_feature"
            ],
            args_log_path=setup_paths,
            args_target=dfe_config_fixtures["global_settings"]["default_target"],
            args_target_file_path=dfe_config_fixtures["global_settings"]["target_path"],
            args_schema_filter_list=schema_name,
        )

        validate_schema_exists(ch_client, org_name, schema_name)

        db_result = ch_client.execute(f"SHOW DATABASES LIKE '{org_name}'")
        assert len(db_result) == 1, f"Database {org_name} was not created"

    try:
        ensure_schema_db(org_name, apply_schema_concurrent)
    except Exception as e:
        pytest.fail(f"Apply schemas with concurrent execution failed: {e}")
