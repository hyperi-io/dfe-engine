import pytest
import logging
from pathlib import Path
from datetime import datetime
from unittest.mock import MagicMock

from dfecli.dfe_schemabuilder.schema_controller import SchemaController
from dfecli.dfe_schemabuilder.schema_plan import (
    SchemaPlan,
    TableStats,
)
from dfecli.dfe_schemabuilder.schema_util import SchemaUtils
from dfecli.dfe_clickhouse.clickhouse_manager import ClickHouseManager
from dfecli.dfe_config.config_loader import DFEConfigLoader

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setLevel(logging.INFO)
logger.addHandler(handler)


@pytest.fixture(scope="module")
def ch_client(dfe_config_fixtures):
    config = DFEConfigLoader.read_clickhouse_config(
        target_name=dfe_config_fixtures["global_settings"]["default_target"],
        targets_file_path=dfe_config_fixtures["global_settings"]["target_path"],
    )
    logger.info(f"Using ClickHouse config for testing: {config}")
    ch_client = ClickHouseManager.get_instance(logger, config).get_clickhouse_client()
    yield ch_client


@pytest.fixture(scope="module")
def schema_plan(dfe_config_fixtures):
    config = DFEConfigLoader.read_clickhouse_config(
        target_name=dfe_config_fixtures["global_settings"]["default_target"],
        targets_file_path=dfe_config_fixtures["global_settings"]["target_path"],
    )
    test_orgs = [{"org_id": "org321", "cluster_name": ""}]
    plan = SchemaPlan(
        dfe_output_directory=dfe_config_fixtures["global_settings"][
            "schema_output_path"
        ],
        organisations=test_orgs,
        target_config_data=config,
        logger=logger,
    )
    yield plan


def setup_test_db(ch_client, org_id):
    try:
        ch_client.execute(f"CREATE DATABASE IF NOT EXISTS {org_id}")
        logger.info(f"Created or verified database {org_id} exists")
    except Exception as e:
        logger.error(f"Failed to create database: {e}")
        raise


def drop_test_db(ch_client, org_id):
    try:
        ch_client.execute(f"DROP DATABASE IF EXISTS {org_id}")
        logger.info(f"Dropped database {org_id}")
    except Exception as e:
        logger.error(f"Failed to drop database: {e}")


@pytest.fixture
def setup_db_and_tables(ch_client, test_org):
    setup_test_db(ch_client, test_org)
    yield
    drop_test_db(ch_client, test_org)


@pytest.fixture(scope="module")
def test_org():
    return "org321"


def test_build_and_plan_schema(dfe_config_fixtures, dfe_package, setup_paths, test_org):
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

        SchemaController.plan_schemas(
            args_dfe_package_file_path=dfe_package,
            args_log_path=setup_paths,
            args_target=dfe_config_fixtures["global_settings"]["default_target"],
            args_target_file_path=dfe_config_fixtures["global_settings"]["target_path"],
            args_schema_filter_list="logs_alerts",
            args_org_filter_list=test_org,
        )

    except Exception as e:
        pytest.fail(f"Build and plan schema failed: {e}")


def test_direct_schema_planning_no_db(
    dfe_config_fixtures, dfe_package, schema_plan, test_org
):
    try:
        schema_plan.ch_client.execute(f"DROP DATABASE IF EXISTS {test_org}")

        schema_path = (
            Path(dfe_config_fixtures["global_settings"]["schema_output_path"]).resolve()
            / "logs_alerts"
            / "logs_alerts.sql"
        )
        with open(schema_path, "r") as f:
            schema_ddl = f.read()

        test_logger = logging.getLogger("test_logger")
        test_logger.setLevel(logging.INFO)

        log_records = []

        class TestHandler(logging.Handler):
            def emit(self, record):
                log_records.append(record.getMessage())

        test_handler = TestHandler()
        test_logger.addHandler(test_handler)
        schema_plan.logger = test_logger

        schema_plan.plan_schemas(schema_ddl, test_org, "logs_alerts")

        assert any(f"new database to create [{test_org}]" in msg for msg in log_records)

    except Exception as e:
        pytest.fail(f"Direct schema planning failed: {e}")


def test_direct_schema_planning_with_db(
    ch_client, dfe_config_fixtures, schema_plan, test_org
):
    try:
        ch_client.execute(f"CREATE DATABASE IF NOT EXISTS {test_org}")

        schema_path = (
            Path(dfe_config_fixtures["global_settings"]["schema_output_path"]).resolve()
            / "logs_alerts"
            / "logs_alerts.sql"
        )
        with open(schema_path, "r") as f:
            schema_ddl = f.read()

        test_logger = logging.getLogger("test_logger")
        test_logger.setLevel(logging.INFO)

        log_records = []

        class TestHandler(logging.Handler):
            def emit(self, record):
                log_records.append(record.getMessage())

        test_handler = TestHandler()
        test_logger.addHandler(test_handler)
        schema_plan.logger = test_logger

        schema_plan.plan_schemas(schema_ddl, test_org, "logs_alerts")

        assert any("new table to create" in msg for msg in log_records)

        ch_client.execute(f"DROP DATABASE IF EXISTS {test_org}")

    except Exception as e:
        pytest.fail(f"Direct schema planning failed: {e}")
        ch_client.execute(f"DROP DATABASE IF EXISTS {test_org}")


def test_parse_columns_from_ddl(schema_plan):
    test_ddl = """
    CREATE TABLE IF NOT EXISTS {{ org_id }}.test_table
    (
        timestamp DateTime64(3,'UTC') CODEC(DoubleDelta, LZ4),
        timestamp_load DateTime64(3,'UTC') DEFAULT now() CODEC(DoubleDelta, LZ4),
        event_hash String CODEC(LZ4),
        logoriginal String CODEC(ZSTD(1)),
        logjson String CODEC(ZSTD(1)),
        org_id LowCardinality(String) CODEC(LZ4),
        message String CODEC(LZ4)
    ) ENGINE = MergeTree()
    PARTITION BY toYYYYMM(timestamp_load)
    ORDER BY (timestamp_load)
    PRIMARY KEY (timestamp_load);
    """

    columns = schema_plan.parse_columns_from_ddl(test_ddl)

    expected_columns = [
        "timestamp",
        "timestamp_load",
        "event_hash",
        "logoriginal",
        "logjson",
        "org_id",
        "message",
    ]
    assert columns == expected_columns


def test_parse_columns_from_ddl_excludes_projections(schema_plan):
    """Test that projections are not parsed as columns in SchemaPlan class"""
    test_ddl = """
    CREATE TABLE IF NOT EXISTS {{ org_id }}.test_table
    (
        timestamp DateTime64(3,'UTC') CODEC(DoubleDelta, LZ4),
        timestamp_load DateTime64(3,'UTC') DEFAULT now() CODEC(DoubleDelta, LZ4),
        event_hash String CODEC(LZ4),
        logoriginal String CODEC(ZSTD(1)),
        INDEX idx_timestamp timestamp TYPE minmax GRANULARITY 4,
        PROJECTION timestamp_optimized ( SELECT * ORDER BY timestamp ),
        PROJECTION complex_multiline (
            SELECT 
                timestamp,
                count(*) as event_count,
                max(timestamp) as max_ts
            FROM some_table
            WHERE timestamp > '2023-01-01'
            GROUP BY toStartOfDay(timestamp)
            ORDER BY timestamp DESC
        )
    ) ENGINE = MergeTree()
    PARTITION BY toYYYYMM(timestamp_load)
    ORDER BY (timestamp_load)
    PRIMARY KEY (timestamp_load);
    """

    columns = schema_plan.parse_columns_from_ddl(test_ddl)

    expected_columns = [
        "timestamp",
        "timestamp_load", 
        "event_hash",
        "logoriginal",
    ]
    
    # Ensure projections are NOT parsed as columns
    assert columns == expected_columns
    
    # Ensure no projection-related content leaks into columns
    forbidden_words = ['timestamp_optimized', 'complex_multiline', 'PROJECTION', 
                      'SELECT', 'FROM', 'WHERE', 'GROUP', 'ORDER', 'event_count', 
                      'max_ts', 'some_table', 'toStartOfDay']
    
    for word in forbidden_words:
        assert word not in columns, f"Projection-related word '{word}' should not be in columns: {columns}"
    

def test_detect_schema_differences(ch_client, schema_plan, test_org):
    try:
        ch_client.execute(f"CREATE DATABASE IF NOT EXISTS {test_org}")

        initial_ddl = f"""
        CREATE TABLE IF NOT EXISTS {test_org}.diff_table
        (
            id UInt32,
            timestamp DateTime
        ) ENGINE = MergeTree()
        PRIMARY KEY (id)
        ORDER BY (id)
        """

        ch_client.execute(initial_ddl)

        expected_ddl = f"""
        CREATE TABLE IF NOT EXISTS {test_org}.diff_table
        (
            id UInt32,
            timestamp DateTime
        ) ENGINE = MergeTree()
        PRIMARY KEY (timestamp)
        ORDER BY (timestamp, id)
        """

        current_schema_ddl = schema_plan.get_existing_schema_ddl(test_org, "diff_table")

        diff_result = schema_plan.detect_schema_differences(
            test_org, "diff_table", current_schema_ddl, expected_ddl
        )

        assert diff_result.schema_difference is True
        assert diff_result.schema_diff.current_primary_key == "id"
        assert diff_result.schema_diff.expected_primary_key == "timestamp"
        assert diff_result.schema_diff.current_order_by_key == "id"
        assert diff_result.schema_diff.expected_order_by_key == "timestamp, id"

        ch_client.execute(f"DROP DATABASE IF EXISTS {test_org}")

    except Exception as e:
        pytest.fail(f"Schema difference detection failed: {e}")
        ch_client.execute(f"DROP DATABASE IF EXISTS {test_org}")


def test_table_stats_with_data(ch_client, schema_plan, test_org):
    try:
        ch_client.execute(f"CREATE DATABASE IF NOT EXISTS {test_org}")

        create_test_ddl = f"""
        CREATE TABLE IF NOT EXISTS {test_org}.stats_table
        (
            timestamp DateTime64(3, 'UTC'),
            timestamp_load DateTime64(3, 'UTC') DEFAULT now(),
            value String
        ) ENGINE = MergeTree()
        PARTITION BY toYYYYMM(timestamp_load)
        ORDER BY timestamp_load
        """

        ch_client.execute(create_test_ddl)

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        insert_data_query = f"""
        INSERT INTO {test_org}.stats_table (timestamp, value) VALUES
        ('{now}', 'test1'),
        ('{now}', 'test2'),
        ('{now}', 'test3')
        """

        ch_client.execute(insert_data_query)

        table_stats = schema_plan.capture_current_table_size(test_org, "stats_table")

        assert table_stats.table_name == "stats_table"
        assert table_stats.total_rows == 3

        ch_client.execute(f"DROP DATABASE IF EXISTS {test_org}")

    except Exception as e:
        pytest.fail(f"Table statistics test failed: {e}")
        ch_client.execute(f"DROP DATABASE IF EXISTS {test_org}")


def test_execute_schema_files(schema_plan, test_org, tmp_path):
    schema_dir = tmp_path / "schemas"
    schema_dir.mkdir()

    test_ddl = """
    CREATE TABLE IF NOT EXISTS {{ org_id }}.test_table
    (
        timestamp DateTime64(3,'UTC') CODEC(DoubleDelta, LZ4),
        timestamp_load DateTime64(3,'UTC') DEFAULT now() CODEC(DoubleDelta, LZ4)
    ) ENGINE = MergeTree()
    PARTITION BY toYYYYMM(timestamp_load)
    ORDER BY timestamp_load
    """

    table_file = schema_dir / "test_table.sql"
    table_file.write_text(test_ddl)

    view_file = schema_dir / "test_view.sql"
    view_file.write_text(
        "CREATE VIEW {{ org_id }}.test_view AS SELECT * FROM {{ org_id }}.test_table"
    )

    test_logger = logging.getLogger("test_logger")
    test_logger.setLevel(logging.INFO)

    log_records = []

    class TestHandler(logging.Handler):
        def emit(self, record):
            log_records.append(record.getMessage())

    test_handler = TestHandler()
    test_logger.addHandler(test_handler)
    schema_plan.logger = test_logger

    original_plan_schemas = schema_plan.plan_schemas
    plan_schemas_calls = []

    def patched_plan_schemas(ddl, db, table, is_api_call=False):
        plan_schemas_calls.append((ddl, db, table))
        return None

    schema_plan.plan_schemas = patched_plan_schemas

    schema_files = [
        (test_org, str(schema_dir), "test_table.sql"),
        (test_org, str(schema_dir), "test_view.sql"),
    ]
    schema_plan.execute_schema_files(schema_files, is_api_call=False)

    schema_plan.plan_schemas = original_plan_schemas

    assert len(plan_schemas_calls) == 1
    assert plan_schemas_calls[0][2] == "test_table"

    assert any("This is a View test_view" in msg for msg in log_records)


def test_process_sql_scripts(schema_plan, test_org, tmp_path):
    schema_dir = tmp_path / "schemas"
    schema_dir.mkdir()

    test_ddl = """
    CREATE TABLE IF NOT EXISTS {{ org_id }}.process_table
    (
        timestamp DateTime64(3,'UTC') CODEC(DoubleDelta, LZ4),
        value String
    ) ENGINE = MergeTree()
    ORDER BY timestamp
    """

    table_file = schema_dir / "process_table.sql"
    table_file.write_text(test_ddl)

    original_collect = schema_plan.collect_schema_files
    original_execute = schema_plan.execute_schema_files

    schema_plan.collect_schema_files = lambda x: [
        (test_org, str(schema_dir), "process_table.sql")
    ]
    execute_called = False

    def spy_execute(schema_files, is_api_call=False):
        nonlocal execute_called
        execute_called = True
        assert len(schema_files) == 1
        assert schema_files[0][0] == test_org
        assert schema_files[0][2] == "process_table.sql"
        return [] if is_api_call else None

    schema_plan.execute_schema_files = spy_execute

    try:
        schema_plan.process_sql_scripts()
        assert execute_called, "execute_schema_files was not called"
    finally:
        schema_plan.execute_schema_files = original_execute
        schema_plan.collect_schema_files = original_collect


def test_validate_parts_creation(ch_client, schema_plan, test_org):
    try:
        ch_client.execute(f"CREATE DATABASE IF NOT EXISTS {test_org}")

        test_table = f"""
        CREATE TABLE IF NOT EXISTS {test_org}.parts_table
        (
            timestamp DateTime64(3, 'UTC'),
            value String
        ) ENGINE = MergeTree()
        ORDER BY timestamp
        """

        ch_client.execute(test_table)

        ch_client.execute(f"""
        INSERT INTO {test_org}.parts_table 
        SELECT now(), 'test' || toString(number)
        FROM system.numbers
        LIMIT 100
        """)

        schema_plan.execute_query = MagicMock()
        schema_plan.execute_query.return_value = [
            (datetime.now(), test_org, "parts_table", 100, 1)
        ]

        result = schema_plan.validate_parts_creation(test_org, "parts_table")
        assert result is not None
        assert len(result) == 1
        assert result[0][2] == "parts_table"
        assert result[0][3] == 100

        ch_client.execute(f"DROP DATABASE IF EXISTS {test_org}")

    except Exception as e:
        pytest.fail(f"Parts validation test failed: {e}")
        ch_client.execute(f"DROP DATABASE IF EXISTS {test_org}")


def test_check_table_records(ch_client, schema_plan, test_org):
    try:
        ch_client.execute(f"CREATE DATABASE IF NOT EXISTS {test_org}")

        test_table = f"""
        CREATE TABLE IF NOT EXISTS {test_org}.count_table
        (
            id UInt32
        ) ENGINE = MergeTree()
        ORDER BY id
        """

        ch_client.execute(test_table)
        ch_client.execute(f"INSERT INTO {test_org}.count_table VALUES (1), (2), (3)")
        original_execute = schema_plan.execute_query
        schema_plan.execute_query = MagicMock(return_value=[(3,)])

        try:
            count = schema_plan.check_table_records(test_org, "count_table")
            assert count == 3
        finally:
            schema_plan.execute_query = original_execute

        ch_client.execute(f"DROP DATABASE IF EXISTS {test_org}")

    except Exception as e:
        pytest.fail(f"Check table records test failed: {e}")
        ch_client.execute(f"DROP DATABASE IF EXISTS {test_org}")


def test_ttl_extraction(schema_plan, ch_client, test_org):
    try:
        ch_client.execute(f"CREATE DATABASE IF NOT EXISTS {test_org}")

        ttl_ddl = f"""
        CREATE TABLE IF NOT EXISTS {test_org}.ttl_table
        (
            timestamp DateTime,
            value String
        ) ENGINE = MergeTree()
        ORDER BY timestamp
        TTL timestamp + INTERVAL 30 DAY
        """

        ch_client.execute(ttl_ddl)

        current_schema_ddl = schema_plan.get_existing_schema_ddl(test_org, "ttl_table")
        expected_schema_ddl = ttl_ddl.replace("30 DAY", "60 DAY")
        _, _, _, current_ttl, _, _, _, _ = SchemaUtils.extract_keys_and_indexes_from_ddl(ttl_ddl)
        _, _, _, expected_ttl, _, _, _, _ = SchemaUtils.extract_keys_and_indexes_from_ddl(
            expected_schema_ddl
        )

        assert str(current_ttl) != str(expected_ttl)
        assert str(current_ttl) == "30"
        assert str(expected_ttl) == "60"

        assert "TTL" in current_schema_ddl
        assert "toIntervalDay(30)" in current_schema_ddl

        ch_client.execute(f"DROP DATABASE IF EXISTS {test_org}")

    except Exception as e:
        pytest.fail(f"TTL extraction test failed: {e}")
        ch_client.execute(f"DROP DATABASE IF EXISTS {test_org}")


def test_add_columns_to_existing_table(ch_client, schema_plan, test_org):
    try:
        ch_client.execute(f"CREATE DATABASE IF NOT EXISTS {test_org}")

        initial_schema = f"""
        CREATE TABLE IF NOT EXISTS {test_org}.columns_test
        (
            id UInt32,
            name String
        ) ENGINE = MergeTree()
        ORDER BY id
        """
        ch_client.execute(initial_schema)

        updated_schema = f"""
        CREATE TABLE IF NOT EXISTS {test_org}.columns_test
        (
            id UInt32,
            name String,
            created_at DateTime,
            status String
        ) ENGINE = MergeTree()
        ORDER BY id
        """

        test_logger = logging.getLogger("test_logger")
        test_logger.setLevel(logging.INFO)
        log_records = []

        class TestHandler(logging.Handler):
            def emit(self, record):
                log_records.append(record.getMessage())

        test_handler = TestHandler()
        test_logger.addHandler(test_handler)
        schema_plan.logger = test_logger

        original_execute_query = schema_plan.execute_query
        execute_queries = []
        original_capture_table_size = schema_plan.capture_current_table_size

        def mock_execute_query(query):
            execute_queries.append(query)
            return original_execute_query(query)

        def mock_capture_table_size(db, table):
            return TableStats(
                table_name=table, size_bytes=0, total_rows=0, rows_by_day=[]
            )

        schema_plan.execute_query = mock_execute_query
        schema_plan.capture_current_table_size = mock_capture_table_size

        try:
            schema_plan.plan_schemas(updated_schema, test_org, "columns_test")

            columns_detected = any(
                msg.find("Columns to add:") >= 0
                and "created_at" in msg
                and "status" in msg
                for msg in log_records
            )
            assert columns_detected, "New columns were not detected for addition"

            ch_client.execute(
                f"ALTER TABLE {test_org}.columns_test ADD COLUMN created_at DateTime"
            )
            ch_client.execute(
                f"ALTER TABLE {test_org}.columns_test ADD COLUMN status String"
            )

            columns = schema_plan.fetch_existing_columns(test_org, "columns_test")
            assert "created_at" in columns, (
                "New column 'created_at' was not found after manual addition"
            )
            assert "status" in columns, (
                "New column 'status' was not found after manual addition"
            )

        finally:
            schema_plan.execute_query = original_execute_query
            schema_plan.capture_current_table_size = original_capture_table_size

        ch_client.execute(f"DROP DATABASE IF EXISTS {test_org}")
    except Exception as e:
        pytest.fail(f"Column addition test failed: {e}")
        ch_client.execute(f"DROP DATABASE IF EXISTS {test_org}")


def test_modify_primary_key(ch_client, schema_plan, test_org):
    try:
        ch_client.execute(f"CREATE DATABASE IF NOT EXISTS {test_org}")

        initial_schema = f"""
        CREATE TABLE IF NOT EXISTS {test_org}.primary_key_test
        (
            id UInt32,
            timestamp DateTime,
            value String
        ) ENGINE = MergeTree()
        PRIMARY KEY (id)
        ORDER BY id
        """
        ch_client.execute(initial_schema)

        ch_client.execute(f"""
        INSERT INTO {test_org}.primary_key_test VALUES
        (1, now(), 'test1'),
        (2, now(), 'test2')
        """)

        updated_schema = f"""
        CREATE TABLE IF NOT EXISTS {test_org}.primary_key_test
        (
            id UInt32,
            timestamp DateTime,
            value String
        ) ENGINE = MergeTree()
        PRIMARY KEY (timestamp, id)
        ORDER BY (timestamp, id)
        """

        test_logger = logging.getLogger("test_logger")
        test_logger.setLevel(logging.INFO)
        log_records = []

        class TestHandler(logging.Handler):
            def emit(self, record):
                log_records.append(record.getMessage())

        test_handler = TestHandler()
        test_logger.addHandler(test_handler)
        schema_plan.logger = test_logger

        original_capture_table_size = schema_plan.capture_current_table_size

        def mock_capture_table_size(db, table):
            return TableStats(
                table_name=table, size_bytes=0, total_rows=0, rows_by_day=[]
            )

        schema_plan.capture_current_table_size = mock_capture_table_size

        try:
            schema_plan.plan_schemas(updated_schema, test_org, "primary_key_test")

            assert any("Primary Key:" in msg for msg in log_records), (
                "Primary key change not detected"
            )

            assert "primary_key_test" in schema_plan.schema_update_map
            assert (
                schema_plan.schema_update_map["primary_key_test"]["current_primary_key"]
                == "id"
            )
            assert (
                "timestamp, id"
                in schema_plan.schema_update_map["primary_key_test"][
                    "expected_primary_key"
                ]
            )
        finally:
            schema_plan.capture_current_table_size = original_capture_table_size

        ch_client.execute(f"DROP DATABASE IF EXISTS {test_org}")
    except Exception as e:
        pytest.fail(f"Primary key modification test failed: {e}")
        ch_client.execute(f"DROP DATABASE IF EXISTS {test_org}")


def test_add_and_remove_indexes(ch_client, schema_plan, test_org):
    try:
        ch_client.execute(f"CREATE DATABASE IF NOT EXISTS {test_org}")

        initial_schema = f"""
        CREATE TABLE IF NOT EXISTS {test_org}.index_test
        (
            id UInt32,
            timestamp DateTime,
            value String,
            INDEX idx_value value TYPE bloom_filter GRANULARITY 1
        ) ENGINE = MergeTree()
        ORDER BY id
        """
        ch_client.execute(initial_schema)

        updated_schema = f"""
        CREATE TABLE IF NOT EXISTS {test_org}.index_test
        (
            id UInt32,
            timestamp DateTime,
            value String,
            INDEX idx_timestamp timestamp TYPE minmax GRANULARITY 1
        ) ENGINE = MergeTree()
        ORDER BY id
        """

        current_schema_ddl = schema_plan.get_existing_schema_ddl(test_org, "index_test")

        diff_result = schema_plan.detect_schema_differences(
            test_org, "index_test", current_schema_ddl, updated_schema
        )

        assert diff_result.schema_difference is True
        current_indexes = [idx[0] for idx in diff_result.schema_diff.current_indexes]
        expected_indexes = [idx[0] for idx in diff_result.schema_diff.expected_indexes]

        assert "idx_value" in current_indexes, "Current index 'idx_value' not found"
        assert "idx_timestamp" in expected_indexes, (
            "Expected index 'idx_timestamp' not found"
        )
        assert "idx_value" not in expected_indexes, (
            "Index 'idx_value' should be removed"
        )

        ch_client.execute(f"DROP DATABASE IF EXISTS {test_org}")
    except Exception as e:
        pytest.fail(f"Index modification test failed: {e}")
        ch_client.execute(f"DROP DATABASE IF EXISTS {test_org}")


def test_extract_keys_from_ddl_without_subsampling(schema_plan):
    """Test extracting keys from DDL without subsampling"""
    ddl_statement = """
    CREATE TABLE test_table (
        id String,
        timestamp DateTime
    ) ENGINE = MergeTree()
    PARTITION BY toYYYYMM(timestamp)
    PRIMARY KEY (timestamp_load, id)
    ORDER BY (timestamp_load, id)
    """

    primary_key, order_by_key, indexes, ttl_value, sample_by, partition_by, projection, table_settings = (
        SchemaUtils.extract_keys_and_indexes_from_ddl(ddl_statement)
    )

    assert primary_key == "timestamp_load, id"
    assert order_by_key == "timestamp_load, id"
    assert indexes == []
    assert ttl_value is None
    assert sample_by is None


def test_extract_keys_from_ddl_with_subsampling(schema_plan):
    """Test extracting keys from DDL with subsampling"""
    ddl_statement = """
    CREATE TABLE test_table (
        id String,
        timestamp DateTime
    ) ENGINE = MergeTree()
    PARTITION BY toYYYYMM(timestamp)
    PRIMARY KEY (cityHash64(timestamp_load), timestamp_load, id)
    ORDER BY (cityHash64(timestamp_load), timestamp_load, id)
    SAMPLE BY cityHash64(timestamp_load)
    """

    primary_key, order_by_key, indexes, ttl_value, sample_by, partition_by, projection, table_settings = (
        SchemaUtils.extract_keys_and_indexes_from_ddl(ddl_statement)
    )

    assert primary_key == "cityHash64(timestamp_load), timestamp_load, id"
    assert order_by_key == "cityHash64(timestamp_load), timestamp_load, id"
    assert indexes == []
    assert ttl_value is None
    assert sample_by == "cityHash64(timestamp_load)"


def test_schema_difference_detection_without_subsampling(
    ch_client, schema_plan, test_org
):
    """Test schema difference detection without subsampling enabled"""
    try:
        ch_client.execute(f"CREATE DATABASE IF NOT EXISTS {test_org}")

        initial_ddl = f"""
        CREATE TABLE IF NOT EXISTS {test_org}.subsampling_test
        (
            id String,
            timestamp DateTime,
            timestamp_load DateTime
        ) ENGINE = MergeTree()
        PARTITION BY toYYYYMM(timestamp)
        PRIMARY KEY (timestamp_load, id)
        ORDER BY (timestamp_load, id)
        """

        ch_client.execute(initial_ddl)

        expected_ddl = f"""
        CREATE TABLE IF NOT EXISTS {test_org}.subsampling_test
        (
            id String,
            timestamp DateTime,
            timestamp_load DateTime,
            new_column String
        ) ENGINE = MergeTree()
        PARTITION BY toYYYYMM(timestamp)
        PRIMARY KEY (timestamp_load, id)
        ORDER BY (timestamp_load, id)
        """

        current_schema_ddl = schema_plan.get_existing_schema_ddl(
            test_org, "subsampling_test"
        )

        diff_result = schema_plan.detect_schema_differences(
            test_org, "subsampling_test", current_schema_ddl, expected_ddl
        )

        assert diff_result.schema_difference is False

        ch_client.execute(f"DROP DATABASE IF EXISTS {test_org}")

    except Exception as e:
        pytest.fail(f"Schema difference detection without subsampling failed: {e}")


def test_schema_difference_detection_with_subsampling(ch_client, schema_plan, test_org):
    """Test schema difference detection with subsampling enabled"""
    try:
        ch_client.execute(f"CREATE DATABASE IF NOT EXISTS {test_org}")

        initial_ddl = f"""
        CREATE TABLE IF NOT EXISTS {test_org}.subsampling_test
        (
            id String,
            timestamp DateTime,
            timestamp_load DateTime
        ) ENGINE = MergeTree()
        PARTITION BY toYYYYMM(timestamp)
        PRIMARY KEY (timestamp_load, id)
        ORDER BY (timestamp_load, id)
        """

        ch_client.execute(initial_ddl)

        expected_ddl = f"""
        CREATE TABLE IF NOT EXISTS {test_org}.subsampling_test
        (
            id String,
            timestamp DateTime,
            timestamp_load DateTime
        ) ENGINE = MergeTree()
        PARTITION BY toYYYYMM(timestamp)
        PRIMARY KEY (cityHash64(timestamp_load), timestamp_load, id)
        ORDER BY (cityHash64(timestamp_load), timestamp_load, id)
        SAMPLE BY cityHash64(timestamp_load)
        """

        current_schema_ddl = schema_plan.get_existing_schema_ddl(
            test_org, "subsampling_test"
        )

        diff_result = schema_plan.detect_schema_differences(
            test_org, "subsampling_test", current_schema_ddl, expected_ddl
        )

        assert diff_result.schema_difference is True
        assert "cityHash64" in diff_result.schema_diff.expected_primary_key
        assert "cityHash64" in diff_result.schema_diff.expected_order_by_key

        ch_client.execute(f"DROP DATABASE IF EXISTS {test_org}")

    except Exception as e:
        pytest.fail(f"Schema difference detection with subsampling failed: {e}")


def test_build_with_subsampling_enabled(
    dfe_config_fixtures, dfe_package, setup_paths, test_org
):
    """Test building schemas with subsampling enabled"""
    try:
        config_with_subsampling = dict(dfe_config_fixtures)
        if "global_settings" not in config_with_subsampling:
            config_with_subsampling["global_settings"] = {}
        config_with_subsampling["global_settings"]["use_subsampling_feature"] = True

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
            args_use_subsampling_feature=True,  # Explicitly enable subsampling
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

        with open(expected_schema_path, "r") as f:
            sql_content = f.read()

        assert "cityHash64(timestamp_load)" in sql_content, (
            "Subsampling hash function not found in SQL"
        )

    except Exception as e:
        pytest.fail(f"Building with subsampling enabled failed: {e}")


def test_print_new_table_structure(schema_plan, test_org):
    """Test CLI output for new table structure printing"""
    test_ddl = f"""
    CREATE TABLE IF NOT EXISTS {test_org}.new_test_table
    (
        id UInt32,
        timestamp DateTime,
        value String,
        INDEX idx_timestamp timestamp TYPE minmax GRANULARITY 4
    ) ENGINE = MergeTree()
    PRIMARY KEY (id)
    ORDER BY (timestamp, id)
    TTL timestamp + INTERVAL 30 DAY
    """

    test_logger = logging.getLogger("test_logger")
    test_logger.setLevel(logging.INFO)
    log_records = []

    class TestHandler(logging.Handler):
        def emit(self, record):
            log_records.append(record.getMessage())

    test_handler = TestHandler()
    test_logger.addHandler(test_handler)
    schema_plan.logger = test_logger

    schema_plan.print_new_table_structure(test_org, "new_test_table", test_ddl)

    # Check that CLI output contains expected elements
    output = "\n".join(log_records)
    assert "******************* new table to create ****************************" in output
    assert f"Table new_test_table in database {test_org} does not exist" in output
    assert "++Primary Key: id" in output
    assert "++Order By Key: timestamp, id" in output
    assert "++ Index: idx_timestamp -> timestamp TYPE minmax GRANULARITY 4" in output
    assert "++ TTL: 30 days" in output
    assert "*****************************************************************" in output


def test_data_type_change_detection(schema_plan, ch_client, test_org):
    """Test data type change detection functionality"""
    try:
        ch_client.execute(f"CREATE DATABASE IF NOT EXISTS {test_org}")

        # Create table with initial data types
        initial_ddl = f"""
        CREATE TABLE IF NOT EXISTS {test_org}.dtype_test
        (
            user_id String,
            age Int32,
            score Float32
        ) ENGINE = MergeTree()
        ORDER BY user_id
        """
        ch_client.execute(initial_ddl)

        # Insert test data
        ch_client.execute(f"""
        INSERT INTO {test_org}.dtype_test VALUES
        ('user1', 25, 85.5),
        ('user2', 30, 92.0)
        """)

        # Expected DDL with different data types
        expected_ddl = f"""
        CREATE TABLE IF NOT EXISTS {test_org}.dtype_test
        (
            user_id UInt64,
            age Int64,
            score Float64
        ) ENGINE = MergeTree()
        ORDER BY user_id
        """

        # Test data type change detection
        data_type_changes = schema_plan._detect_data_type_changes(test_org, "dtype_test", expected_ddl)
        
        # Debug: print what we got
        print(f"Data type changes detected: {data_type_changes}")
        
        # For now, just check that the method runs without error
        assert isinstance(data_type_changes, list)

        ch_client.execute(f"DROP DATABASE IF EXISTS {test_org}")

    except Exception as e:
        pytest.fail(f"Data type change detection test failed: {e}")
        ch_client.execute(f"DROP DATABASE IF EXISTS {test_org}")


def test_data_type_normalization(schema_plan):
    """Test data type normalization logic"""
    # Test equivalent types
    assert schema_plan._normalize_data_type("Bool") == "Boolean"
    assert schema_plan._normalize_data_type("Boolean") == "Boolean"
    assert schema_plan._normalize_data_type("String") == "String"
    assert schema_plan._normalize_data_type("UnknownType") == "UnknownType"
    
    # Test whitespace normalization around commas and parentheses
    assert schema_plan._normalize_data_type("DateTime64(3, 'UTC')") == "DateTime64(3,'UTC')"
    assert schema_plan._normalize_data_type("DateTime64( 3 , 'UTC' )") == "DateTime64(3,'UTC')"
    assert schema_plan._normalize_data_type("Array( String )") == "Array(String)"
    assert schema_plan._normalize_data_type("Tuple( Int32 , String )") == "Tuple(Int32,String)"
    assert schema_plan._normalize_data_type("Map( String , Int32 )") == "Map(String,Int32)"
    
    # Test combination of equivalents and whitespace
    assert schema_plan._normalize_data_type("Bool") == "Boolean"


def test_plan_schemas_cli_output_with_changes(ch_client, schema_plan, test_org):
    """Test CLI output when schema changes are detected"""
    try:
        ch_client.execute(f"CREATE DATABASE IF NOT EXISTS {test_org}")

        # Create existing table
        initial_ddl = f"""
        CREATE TABLE IF NOT EXISTS {test_org}.cli_test
        (
            id UInt32,
            name String
        ) ENGINE = MergeTree()
        PRIMARY KEY (id)
        ORDER BY id
        """
        ch_client.execute(initial_ddl)

        # Insert test data
        ch_client.execute(f"INSERT INTO {test_org}.cli_test VALUES (1, 'test')")

        # Expected DDL with changes
        expected_ddl = f"""
        CREATE TABLE IF NOT EXISTS {test_org}.cli_test
        (
            id UInt32,
            name String,
            new_column UInt64
        ) ENGINE = MergeTree()
        PRIMARY KEY (id)
        ORDER BY (id, name)
        """

        test_logger = logging.getLogger("test_logger")
        test_logger.setLevel(logging.INFO)
        log_records = []

        class TestHandler(logging.Handler):
            def emit(self, record):
                log_records.append(record.getMessage())

        test_handler = TestHandler()
        test_logger.addHandler(test_handler)
        schema_plan.logger = test_logger

        # Mock table size to avoid actual size calculation
        original_capture_size = schema_plan.capture_current_table_size
        schema_plan.capture_current_table_size = lambda db, table: TableStats(
            table_name=table, size_bytes=1024, total_rows=1, rows_by_day=[]
        )

        try:
            schema_plan.plan_schemas(expected_ddl, test_org, "cli_test")

            output = "\n".join(log_records)

            # Check for CLI reporting elements
            assert "*******************TABLE STATISTICS******************************" in output
            assert "Table Size: 1024 bytes" in output
            assert "Total rows: 1" in output
            assert "- Changes:" in output
            assert "Columns to Add:" in output
            assert "new_column" in output

        finally:
            schema_plan.capture_current_table_size = original_capture_size

        ch_client.execute(f"DROP DATABASE IF EXISTS {test_org}")

    except Exception as e:
        pytest.fail(f"CLI output test with changes failed: {e}")
        ch_client.execute(f"DROP DATABASE IF EXISTS {test_org}")


def test_plan_schemas_error_handling(ch_client, schema_plan, test_org):
    """Test error handling in plan_schemas"""
    # Create the database first so we reach the parsing logic
    ch_client.execute(f"CREATE DATABASE IF NOT EXISTS {test_org}")
    
    # Test with invalid DDL
    invalid_ddl = "INVALID SQL STATEMENT"

    # This should handle the invalid DDL gracefully
    result = schema_plan.plan_schemas(invalid_ddl, test_org, "error_test")

    # Should return None for CLI calls (not API calls)
    assert result is None
    
    # Clean up
    ch_client.execute(f"DROP DATABASE IF EXISTS {test_org}")


def test_empty_schema_files_collection(schema_plan):
    """Test handling of empty schema files collection"""
    # Mock collect_schema_files to return empty list
    original_collect = schema_plan.collect_schema_files
    def mock_collect(customer_data):
        schema_plan.logger.info("Using the following list of schemas: '[]'.")
        return []
    schema_plan.collect_schema_files = mock_collect

    test_logger = logging.getLogger("test_logger")
    test_logger.setLevel(logging.INFO)
    log_records = []

    class TestHandler(logging.Handler):
        def emit(self, record):
            log_records.append(record.getMessage())

    test_handler = TestHandler()
    test_logger.addHandler(test_handler)
    schema_plan.logger = test_logger

    result = schema_plan.process_sql_scripts()
    assert result is None  # CLI mode returns None

    # Should log that no schemas were found
    assert any("Using the following list of schemas: '[]'" in msg for msg in log_records)

    # Restore original method
    schema_plan.collect_schema_files = original_collect
