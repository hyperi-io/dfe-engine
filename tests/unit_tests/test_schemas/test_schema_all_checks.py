import re
import uuid
import pytest
import logging
from dfecli.dfe_schemabuilder.schema_controller import SchemaModifier
from unittest.mock import MagicMock, patch

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
handler = logging.StreamHandler()
handler.setLevel(logging.DEBUG)
logger.addHandler(handler)


@pytest.fixture
def update_schemas(dfe_config_fixtures, target_config_data):
    dfe_output_directory = dfe_config_fixtures["global_settings"]["schema_output_path"]
    organisations = dfe_config_fixtures.get("organisations", [])
    use_json_feature = dfe_config_fixtures["global_settings"].get(
        "use_json_feature", False
    )
    use_subsampling_feature = dfe_config_fixtures["global_settings"].get(
        "use_subsampling_feature", False
    )
    return SchemaModifier(
        dfe_output_directory=dfe_output_directory,
        schema_update_flag="YES",
        drop_replacement_table_flag="YES",
        use_replicated_merge_tree=dfe_config_fixtures["build_schemas"][
            "use_replicated_merge_tree"
        ],
        use_json_feature=use_json_feature,
        use_subsampling_feature=use_subsampling_feature,
        use_shared_merge_tree=dfe_config_fixtures["build_schemas"][
            "use_shared_merge_tree"
        ],
        do_add_columns=dfe_config_fixtures["apply_schemas"]["do_add_columns"],
        organisations=organisations,
        target_config_data=target_config_data,
        logger=logger,
        schema_filter_list="logs_alerts",
        max_insert_threads=8,
        min_insert_block_size_rows=1048576,
    )


def test_detect_schema_differences(update_schemas):
    update_schemas.ch_client = MagicMock()
    update_schemas.ch_client.execute = MagicMock()
    update_schemas.ch_client.execute.return_value = [("id", "timestamp")]
    current_schema_ddl = """
    CREATE TABLE test_db.test_table (
        id UInt32,
        name String,
        timestamp DateTime,
        INDEX idx_name name TYPE minmax GRANULARITY 1
    ) ENGINE = MergeTree()
    PRIMARY KEY (id)
    ORDER BY (timestamp);
    """
    expected_schema_ddl = """
    CREATE TABLE test_db.test_table (
        id UInt32,
        name String,
        timestamp DateTime,
        INDEX idx_name name TYPE minmax GRANULARITY 1
    ) ENGINE = MergeTree()
    PRIMARY KEY (id)
    ORDER BY (timestamp);
    """

    (
        schema_difference,
        expected_primary_key,
        expected_order_by,
        current_indexes,
        expected_indexes,
        ttl_value,
        expected_sample_by,
        current_primary_key,
        current_order_by_key,
        current_sample_by,
    ) = update_schemas.detect_schema_differences(
        "test_db", "test_table", current_schema_ddl, expected_schema_ddl
    )

    assert not schema_difference, (
        f"Schema differences detected: Primary Key: {expected_primary_key}, "
        f"Order By: {expected_order_by}, Indexes: {expected_indexes}"
    )
    assert expected_primary_key == "id"
    assert expected_order_by == "timestamp"
    assert expected_indexes == [("idx_name", "name TYPE minmax GRANULARITY 1")]
    assert expected_sample_by == ""  # Add assertion for sample_by


def test_detect_schema_differences_without_parentheses(update_schemas):
    update_schemas.ch_client = MagicMock()
    update_schemas.ch_client.execute = MagicMock()
    update_schemas.ch_client.execute.return_value = [("id", "timestamp")]

    current_schema_ddl = """
    CREATE TABLE test_db.test_table (
        id UInt32,
        name String,
        timestamp DateTime,
        INDEX idx_timestamp timestamp TYPE set(0) GRANULARITY 4
    ) ENGINE = MergeTree()
    PRIMARY KEY (id)
    ORDER BY (timestamp);
    """
    expected_schema_ddl = """
    CREATE TABLE test_db.test_table (
        id UInt32,
        name String,
        timestamp DateTime,
        INDEX idx_timestamp(timestamp) TYPE set(0) GRANULARITY 4
    ) ENGINE = MergeTree()
    PRIMARY KEY (id)
    ORDER BY (timestamp);
    """

    (
        schema_difference,
        expected_primary_key,
        expected_order_by,
        current_indexes,
        expected_indexes,
        ttl_value,
        expected_sample_by,
        current_primary_key,
        current_order_by_key,
        current_sample_by,
    ) = update_schemas.detect_schema_differences(
        "test_db", "test_table", current_schema_ddl, expected_schema_ddl
    )

    assert not schema_difference, (
        f"Schema differences detected when only the index format differs. "
        f"Current Indexes: {current_indexes}, Expected Indexes: {expected_indexes}"
    )

    assert len(current_indexes) == 1, "Expected one index in current schema"
    assert len(expected_indexes) == 1, "Expected one index in expected schema"
    assert current_indexes[0][0] == "idx_timestamp", (
        f"Current index name mismatch: {current_indexes[0][0]}"
    )
    assert expected_indexes[0][0] == "idx_timestamp", (
        f"Expected index name mismatch: {expected_indexes[0][0]}"
    )
    assert current_indexes[0][1] == "timestamp TYPE set(0) GRANULARITY 4", (
        f"Current index details mismatch: {current_indexes[0][1]}"
    )
    assert expected_indexes[0][1] == "timestamp TYPE set(0) GRANULARITY 4", (
        f"Expected index details mismatch: {expected_indexes[0][1]}"
    )


def test_create_replacement_table(update_schemas):
    update_schemas.table_exists = MagicMock(side_effect=[False, True])
    update_schemas.ch_client = MagicMock()
    update_schemas.ch_client.execute = MagicMock()

    create_table_query = """
    CREATE TABLE test_db.test_table (
        id UInt32,
        name String,
        timestamp DateTime
    ) ENGINE = MergeTree()
    PRIMARY KEY (timestamp,id)
    ORDER BY (timestamp,id);
    """
    update_schemas.ch_client.execute(create_table_query)

    update_schemas.ch_client.execute.return_value = [
        ("timestamp", "DateTime"),
        ("id", "UInt32"),
        ("name", "String"),
    ]

    update_schemas.execute_query = MagicMock(return_value=[(0,)])

    primary_key = "timestamp, id"
    order_by = "timestamp, id"
    ttl_value = "7"

    update_schemas.create_replacement_table(
        "test_db", "test_table", primary_key, order_by, "", ttl_value
    )

    expected_query = """
                CREATE TABLE test_db.replacement_test_table AS test_db.test_table
                ENGINE = MergeTree()
                PARTITION BY toYYYYMMDD(timestamp_load)
                PRIMARY KEY (timestamp_load, timestamp, id)
                ORDER BY (timestamp_load, timestamp, id)
                TTL timestamp + INTERVAL 7 DAY DELETE WHERE timestamp >= 0,
                timestamp_load + INTERVAL 7 DAY DELETE WHERE timestamp_load >= 0
                SETTINGS
    index_granularity = 2048,
    ttl_only_drop_parts = 1;
    """.strip()

    def normalize_whitespace(query):
        return re.sub(r"\s+", " ", query).strip()

    update_schemas.execute_query.assert_called()
    actual_query = update_schemas.execute_query.call_args[0][0]
    assert normalize_whitespace(actual_query) == normalize_whitespace(expected_query), (
        f"Expected:\n{expected_query}\nBut got:\n{actual_query}"
    )


def test_create_replacement_table_subsampling(update_schemas):
    update_schemas.table_exists = MagicMock(side_effect=[False, True])
    update_schemas.ch_client = MagicMock()
    update_schemas.ch_client.execute = MagicMock()

    create_table_query = """
    CREATE TABLE test_db.test_table (
        id UInt32,
        name String,
        timestamp DateTime
    ) ENGINE = MergeTree()
    PRIMARY KEY (timestamp,id)
    ORDER BY (timestamp,id);
    """
    update_schemas.ch_client.execute(create_table_query)

    update_schemas.ch_client.execute.return_value = [
        ("timestamp", "DateTime"),
        ("id", "UInt32"),
        ("name", "String"),
    ]

    update_schemas.execute_query = MagicMock(return_value=[(0,)])

    primary_key = "timestamp, id"
    order_by = "timestamp, id"
    sample_by = "cityHash64(timestamp_load)"
    ttl_value = "7"

    update_schemas.use_subsampling_feature = True

    update_schemas.create_replacement_table(
        "test_db", "test_table", primary_key, order_by, sample_by, ttl_value
    )

    expected_query = """
                CREATE TABLE test_db.replacement_test_table AS test_db.test_table
                ENGINE = MergeTree()
                PARTITION BY toYYYYMMDD(timestamp_load)
                PRIMARY KEY (cityHash64(timestamp_load), timestamp_load, timestamp, id)
                ORDER BY (cityHash64(timestamp_load), timestamp_load, timestamp, id)
                SAMPLE BY cityHash64(timestamp_load)
                TTL timestamp + INTERVAL 7 DAY DELETE WHERE timestamp >= 0,
                timestamp_load + INTERVAL 7 DAY DELETE WHERE timestamp_load >= 0
                SETTINGS
    index_granularity = 2048,
    ttl_only_drop_parts = 1;
    """.strip()

    def normalize_whitespace(query):
        return re.sub(r"\s+", " ", query).strip()

    update_schemas.execute_query.assert_called()
    actual_query = update_schemas.execute_query.call_args[0][0]
    assert normalize_whitespace(actual_query) == normalize_whitespace(expected_query), (
        f"Expected:\n{expected_query}\nBut got:\n{actual_query}"
    )


def test_exchange_tables(update_schemas):
    database_name = "test_db"
    table_name = "test_table"
    update_schemas.ch_client.execute = MagicMock()
    update_schemas.exchange_tables(database_name, table_name)
    expected_query = (
        f"""
        EXCHANGE TABLES {database_name}.{table_name} AND {database_name}.replacement_{table_name}
    """.strip()
        + ";"
    )

    def normalize_whitespace(query):
        return re.sub(r"\s+", " ", query).strip()

    normalized_actual = normalize_whitespace(
        update_schemas.ch_client.execute.call_args[0][0].rstrip(";")
    )
    normalized_expected = normalize_whitespace(expected_query.rstrip(";"))
    assert normalized_actual == normalized_expected, (
        f"Expected:\n{expected_query}\nBut got:\n{update_schemas.ch_client.execute.call_args[0][0]}"
    )


def test_insert_records_into_new_table(update_schemas):
    database_name = "test_db"
    table_name = "test_table"
    migration_query_id = str(uuid.uuid4())
    update_schemas.ch_client.execute = MagicMock()
    update_schemas.insert_records_into_new_table(
        database_name, table_name, query_id=migration_query_id
    )

    expected_query = """INSERT INTO test_db.test_table SELECT * FROM test_db.replacement_test_table SETTINGS min_insert_block_size_rows = 1048576, max_insert_threads = 8;"""

    def normalize_whitespace(query):
        return re.sub(r"\s+", " ", query).strip()

    actual_query = update_schemas.ch_client.execute.call_args[0][0]
    normalized_actual = normalize_whitespace(actual_query)
    normalized_expected = normalize_whitespace(expected_query)

    assert normalized_actual == normalized_expected, (
        f"Expected:\n{normalized_expected}\nBut got:\n{normalized_actual}"
    )


def test_validate_data_migration(update_schemas):
    database_name = "test_db"
    table_name = "test_table"
    update_schemas.check_table_records = MagicMock(return_value=100)
    update_schemas.validate_data_migration(database_name, table_name)
    update_schemas.check_table_records.assert_any_call(database_name, table_name)
    update_schemas.check_table_records.assert_any_call(
        database_name, f"replacement_{table_name}"
    )


def test_drop_replacement_table(update_schemas):
    database_name = "test_db"
    table_name = "test_table"
    update_schemas.ch_client.execute = MagicMock()
    update_schemas.drop_replacement_table(database_name, table_name)
    expected_query = f"""
        DROP TABLE IF EXISTS {database_name}.replacement_{table_name};
    """.strip()

    def normalize_whitespace(query):
        return re.sub(r"\s+", " ", query).strip()

    normalized_actual = normalize_whitespace(
        update_schemas.ch_client.execute.call_args[0][0]
    )
    normalized_expected = normalize_whitespace(expected_query)
    assert normalized_actual == normalized_expected, (
        f"Expected:\n{expected_query}\nBut got:\n{update_schemas.ch_client.execute.call_args[0][0]}"
    )


def test_get_existing_schema_ddl(update_schemas):
    database_name = "test_db"
    table_name = "test_table"
    update_schemas.ch_client.execute = MagicMock(return_value=[("CREATE TABLE ...",)])
    ddl = update_schemas.get_existing_schema_ddl(database_name, table_name)
    update_schemas.ch_client.execute.assert_called_once_with(
        f"SHOW CREATE TABLE {database_name}.{table_name}"
    )
    assert ddl == "CREATE TABLE ...", (
        "The DDL statement retrieved does not match the expected value."
    )


def test_add_single_index_type(update_schemas):
    update_schemas.ch_client.execute = MagicMock()
    current_indexes = [("idx_existing", "(existing_column) TYPE minmax GRANULARITY 1")]
    expected_indexes = [
        ("idx_existing", "(existing_column) TYPE minmax GRANULARITY 1"),
        ("idx_new", "(new_column) TYPE bloom_filter GRANULARITY 1"),
    ]
    update_schemas.add_indexes(
        "test_db", "test_table", current_indexes, expected_indexes
    )

    expected_query = """
        ALTER TABLE test_db.test_table
        ADD INDEX idx_new (new_column) TYPE bloom_filter GRANULARITY 1
    """.strip()

    normalized_expected_query = re.sub(r"\s+", " ", expected_query.strip())
    normalized_actual_query = re.sub(
        r"\s+", " ", update_schemas.ch_client.execute.call_args[0][0].strip()
    )
    assert normalized_actual_query == normalized_expected_query


def test_add_new_order_by(update_schemas):
    update_schemas.ch_client.execute = MagicMock()
    current_schema_ddl = """
    CREATE TABLE test_db.test_table (
        id UInt32,
        name String,
        timestamp DateTime
    ) ENGINE = MergeTree()
    PRIMARY KEY (id)
    ORDER BY (id);
    """
    expected_schema_ddl = """
    CREATE TABLE test_db.test_table (
        id UInt32,
        name String,
        timestamp DateTime
    ) ENGINE = MergeTree()
    PRIMARY KEY (id)
    ORDER BY (id, timestamp);
    """
    update_schemas.detect_schema_differences = MagicMock(
        return_value=(True, "id", "(id, timestamp)", [], [], None)
    )
    schema_difference, expected_primary_key, expected_order_by, _, _, _ = (
        update_schemas.detect_schema_differences(
            "test_db", "test_table", current_schema_ddl, expected_schema_ddl
        )
    )
    assert schema_difference
    assert expected_order_by == "(id, timestamp)"


def test_add_new_order_by_and_index_type(update_schemas):
    update_schemas.ch_client.execute = MagicMock()
    current_schema_ddl = """
    CREATE TABLE test_db.test_table (
        id UInt32,
        name String,
        timestamp DateTime
    ) ENGINE = MergeTree()
    PRIMARY KEY (id)
    ORDER BY (id);
    """
    expected_schema_ddl = """
    CREATE TABLE test_db.test_table (
        id UInt32,
        name String,
        timestamp DateTime,
        INDEX idx_name (name) TYPE minmax GRANULARITY 1
    ) ENGINE = MergeTree()
    PRIMARY KEY (id)
    ORDER BY (id, timestamp);
    """
    update_schemas.detect_schema_differences = MagicMock(
        return_value=(
            True,
            "id",
            "(id, timestamp)",
            [],
            [("idx_name", "(name) TYPE minmax GRANULARITY 1")],
            None,
        )
    )
    (
        schema_difference,
        expected_primary_key,
        expected_order_by,
        _,
        expected_indexes,
        _,
    ) = update_schemas.detect_schema_differences(
        "test_db", "test_table", current_schema_ddl, expected_schema_ddl
    )
    assert schema_difference
    assert expected_order_by == "(id, timestamp)"
    assert expected_indexes == [("idx_name", "(name) TYPE minmax GRANULARITY 1")]


def test_table_with_nullable_pk_columns(update_schemas, caplog):
    update_schemas.table_exists = MagicMock(return_value=False)
    update_schemas.ch_client = MagicMock()
    update_schemas.ch_client.execute = MagicMock(
        return_value=[
            ("id", "Nullable(UInt32)"),
            ("name", "String"),
            ("timestamp", "DateTime"),
        ]
    )

    update_schemas.execute_query = MagicMock(return_value=[(0,)])

    with caplog.at_level(logging.ERROR):
        primary_key = "id, name"

        is_nullable = update_schemas.check_nullable_primary_key(
            "test_db", "test_table", primary_key
        )
    assert is_nullable, "Expected nullable primary key column check to return True"
    assert "Column 'id' in primary key is nullable" in caplog.text


def test_compare_columns_and_data_types(update_schemas, caplog):
    columns_with_existing_datatypes = {
        "id": "UInt32",
        "name": "String",
        "timestamp": "DateTime",
    }
    columns_with_new_datatypes = {
        "id": "UInt32",
        "name": "FixedString(64)",
        "timestamp": "DateTime",
    }

    update_schemas.compare_columns_and_data_types(
        columns_with_existing_datatypes=columns_with_existing_datatypes,
        columns_with_new_datatypes=columns_with_new_datatypes,
        database_name="test_db",
        table_name="test_table",
    )

    with caplog.at_level("ERROR"):
        update_schemas.compare_columns_and_data_types(
            columns_with_existing_datatypes=columns_with_existing_datatypes,
            columns_with_new_datatypes=columns_with_new_datatypes,
            database_name="test_db",
            table_name="test_table",
        )
    expected_message = (
        "Data type mismatch detected for table 'test_db.test_table'. "
        "Manual intervention required for column 'name'. "
        "Existing data type: 'String', "
        "new data type: 'FixedString(64)'. Please handle the data type change manually."
    )

    assert any(record.message == expected_message for record in caplog.records), (
        f"Expected log message not found. Captured messages: {[record.message for record in caplog.records]}"
    )


def test_remove_indexes(update_schemas):
    """Test removing indexes that are in current schema but not in expected schema."""
    update_schemas.ch_client.execute = MagicMock()
    current_indexes = [
        ("idx_to_keep", "(column1) TYPE minmax GRANULARITY 1"),
        ("idx_to_remove", "(column2) TYPE set(0) GRANULARITY 2"),
    ]
    expected_indexes = [("idx_to_keep", "(column1) TYPE minmax GRANULARITY 1")]

    update_schemas.remove_indexes(
        "test_db", "test_table", current_indexes, expected_indexes
    )

    expected_query = """
        ALTER TABLE test_db.test_table
        DROP INDEX idx_to_remove
    """.strip()

    normalized_expected_query = re.sub(r"\s+", " ", expected_query.strip())
    normalized_actual_query = re.sub(
        r"\s+", " ", update_schemas.ch_client.execute.call_args[0][0].strip()
    )
    assert normalized_actual_query == normalized_expected_query


def test_compare_indexes(update_schemas):
    """Test the comparison of current and expected indexes."""
    current_indexes = [
        ("idx_keep", "(col1) TYPE minmax GRANULARITY 1"),
        ("idx_remove", "(col2) TYPE set(0) GRANULARITY 2"),
        ("idx_modify", "(col3) TYPE minmax GRANULARITY 1"),
    ]
    expected_indexes = [
        ("idx_keep", "(col1) TYPE minmax GRANULARITY 1"),
        ("idx_add", "(col4) TYPE set(0) GRANULARITY 3"),
        ("idx_modify", "(col3) TYPE bloom_filter GRANULARITY 2"),
    ]

    result = update_schemas.compare_indexes(current_indexes, expected_indexes)

    assert len(result["added"]) == 1
    assert result["added"][0][0] == "idx_add"

    assert len(result["removed"]) == 1
    assert result["removed"][0][0] == "idx_remove"

    assert len(result["modified"]) == 1
    assert result["modified"][0][0][0] == "idx_modify"
    assert result["modified"][0][1][0] == "idx_modify"


def test_parse_columns_from_ddl(update_schemas):
    """Test parsing column names from DDL statement."""
    ddl = """
    CREATE TABLE test_db.test_table (
        id UInt32,
        name String,
        timestamp DateTime,
        complex_column Array(UInt8),
        INDEX idx_name name TYPE minmax GRANULARITY 1
    ) ENGINE = MergeTree()
    """

    columns = update_schemas.parse_columns_from_ddl(ddl)
    assert set(columns) == {"id", "name", "timestamp", "complex_column"}


def test_parse_columns_from_ddl_with_projections(update_schemas):
    """Test parsing column names from DDL statement with projections - projections should be excluded."""
    ddl = """
    CREATE TABLE test_db.test_table (
        id UInt32,
        name String,
        timestamp DateTime,
        complex_column Array(UInt8),
        INDEX idx_name name TYPE minmax GRANULARITY 1,
        PROJECTION timestamp_optimized ( SELECT * ORDER BY timestamp ),
        PROJECTION complex_proj (
            SELECT 
                id, 
                count() as cnt,
                avg(id) as avg_id
            FROM some_table
            WHERE timestamp > '2023-01-01'
            GROUP BY id
            ORDER BY id
        ),
        PROJECTION another_proj (
            SELECT name, count() 
            GROUP BY name
        )
    ) ENGINE = MergeTree()
    """

    columns = update_schemas.parse_columns_from_ddl(ddl)
    expected_columns = {"id", "name", "timestamp", "complex_column"}
    assert set(columns) == expected_columns
    
    forbidden_words = ['timestamp_optimized', 'complex_proj', 'another_proj', 'PROJECTION', 
                      'SELECT', 'FROM', 'WHERE', 'GROUP', 'ORDER', 'cnt', 'avg_id', 'some_table']
    
    for word in forbidden_words:
        assert word not in columns, f"Projection-related word '{word}' should not be in columns: {columns}"


def test_get_columns_with_types(update_schemas):
    """Test extracting columns with their types from DDL statement."""
    ddl = """
    CREATE TABLE test_db.test_table (
        id UInt32,
        name String,
        timestamp DateTime,
        nullable_field Nullable(UInt32)
    ) ENGINE = MergeTree()
    """

    columns_with_types = update_schemas.get_columns_with_types(ddl)
    assert columns_with_types == {
        "id": "UInt32",
        "name": "String",
        "timestamp": "DateTime",
        "nullable_field": "Nullable(UInt32)",
    }


def test_generate_alter_statements(update_schemas):
    """Test generating ALTER statements for new columns."""
    existing_columns = ["id", "name"]
    new_columns = ["id", "name", "timestamp", "new_field"]
    ddl = """
    CREATE TABLE test_db.test_table (
        id UInt32,
        name String,
        timestamp DateTime,
        new_field UInt64
    ) ENGINE = MergeTree()
    """

    statements = update_schemas.generate_alter_statements(
        existing_columns, new_columns, "test_db", "test_table", ddl
    )

    assert len(statements) == 1
    assert (
        "ADD COLUMN timestamp DateTime, ADD COLUMN new_field UInt64" in statements[0]
        or "ADD COLUMN new_field UInt64, ADD COLUMN timestamp DateTime" in statements[0]
    )


def test_generate_alter_statements_with_projections(update_schemas):
    """Test generating ALTER statements excludes projection lines from column definitions."""
    existing_columns = ["id", "name"]
    new_columns = ["id", "name", "timestamp", "new_field"]
    ddl = """
    CREATE TABLE test_db.test_table (
        id UInt32,
        name String,
        timestamp DateTime,
        new_field UInt64,
        INDEX idx_timestamp timestamp TYPE minmax GRANULARITY 4,
        PROJECTION timestamp_optimized ( SELECT * ORDER BY timestamp ),
        PROJECTION aggregated_proj (
            SELECT id, count() as cnt
            GROUP BY id
            ORDER BY id
        )
    ) ENGINE = MergeTree()
    """

    statements = update_schemas.generate_alter_statements(
        existing_columns, new_columns, "test_db", "test_table", ddl
    )

    assert len(statements) == 1
    statement = statements[0]
    
    assert "timestamp DateTime" in statement
    assert "new_field UInt64" in statement
    
    assert "PROJECTION" not in statement
    assert "timestamp_optimized" not in statement
    assert "aggregated_proj" not in statement
    assert "SELECT * ORDER BY timestamp" not in statement
    assert "GROUP BY id" not in statement


def test_normalize_data_type(update_schemas):
    """Test normalizing data type names."""
    assert update_schemas.normalize_data_type("Bool") == "Boolean"
    assert update_schemas.normalize_data_type("Nullable(Bool)") == "Nullable(Boolean)"
    assert (
        update_schemas.normalize_data_type("UInt32") == "UInt32"
    )  # No change expected


def test_create_replacement_table_with_sample_by_to_none(update_schemas):
    """Test handling the case when converting from SAMPLE BY to no SAMPLE BY."""
    update_schemas.table_exists = MagicMock(side_effect=[False, True])
    update_schemas.get_existing_schema_ddl = MagicMock(
        return_value="""
    CREATE TABLE test_db.test_table (
        id UInt32,
        timestamp_load DateTime
    ) ENGINE = MergeTree()
    PRIMARY KEY (cityHash64(timestamp_load), id)
    ORDER BY (cityHash64(timestamp_load), id)
    SAMPLE BY cityHash64(timestamp_load)
    """
    )

    update_schemas.ch_client.execute = MagicMock()
    with patch(
        "dfecli.dfe_schemabuilder.schema_util.SchemaUtils.extract_keys_and_indexes_from_ddl"
    ) as mock_extract:
        mock_extract.return_value = (None, None, [], None, "cityHash64(timestamp_load)", None, None, None)
        update_schemas.execute_query = MagicMock(return_value=[(0,)])

        update_schemas.create_replacement_table(
            "test_db", "test_table", "id", "id", "", "7"
        )

        query = update_schemas.execute_query.call_args[0][0]
        assert "SAMPLE BY cityHash64(timestamp_load)" in query
        assert "PRIMARY KEY (timestamp_load, id)" in query


def test_ensure_primary_key_is_prefix_of_order_by(update_schemas):
    """Test ensuring PRIMARY KEY is a prefix of ORDER BY."""
    update_schemas.table_exists = MagicMock(side_effect=[False, True])
    update_schemas.get_existing_schema_ddl = MagicMock(return_value="")
    update_schemas.ch_client.execute = MagicMock()
    with patch(
        "dfecli.dfe_schemabuilder.schema_util.SchemaUtils.extract_keys_and_indexes_from_ddl"
    ) as mock_extract:
        mock_extract.return_value = (None, None, [], None, "", None, None, None)
        update_schemas.execute_query = MagicMock(return_value=[(0,)])

        update_schemas.create_replacement_table(
            "test_db", "test_table", "id, timestamp", "timestamp, other_field", "", "7"
        )

        query = update_schemas.execute_query.call_args[0][0]
        assert "PRIMARY KEY (timestamp_load, timestamp)" in query
        assert (
            "ORDER BY (timestamp_load, timestamp, other_field)" in query
        )  # Should be adjusted to match PRIMARY KEY


def test_create_replacement_table_database_not_exists(update_schemas):
    """Test replacement table creation when database doesn't exist."""
    update_schemas.table_exists = MagicMock(return_value=False)
    update_schemas.ch_client = MagicMock()
    update_schemas.ch_client.execute = MagicMock(side_effect=Exception("Database 'nonexistent_db' does not exist"))

    with pytest.raises(Exception, match="An error occurred in creating replacement table: Database does not exist"):
        update_schemas.create_replacement_table(
            "nonexistent_db", "test_table", "id", "timestamp", "", "1"
        )


def test_create_replacement_table_insufficient_permissions(update_schemas):
    """Test replacement table creation with insufficient permissions."""
    update_schemas.table_exists = MagicMock(return_value=False)
    update_schemas.ch_client = MagicMock()
    update_schemas.ch_client.execute = MagicMock(side_effect=Exception("Not enough privileges"))

    with pytest.raises(Exception, match="An error occurred in creating replacement table: Database operation failed. Ensure schemas are deployed first using apply-schemas command before running updates. Check server logs for technical details if the issue persists."):
        update_schemas.create_replacement_table(
            "test_db", "test_table", "id", "timestamp", "", "1"
        )


def test_create_replacement_table_disk_space_error(update_schemas):
    """Test replacement table creation when disk space is insufficient."""
    update_schemas.table_exists = MagicMock(return_value=False)
    update_schemas.ch_client = MagicMock()
    update_schemas.ch_client.execute = MagicMock(side_effect=Exception("Not enough space on disk"))

    with pytest.raises(Exception, match="An error occurred in creating replacement table: Database operation failed. Ensure schemas are deployed first using apply-schemas command before running updates. Check server logs for technical details if the issue persists."):
        update_schemas.create_replacement_table(
            "test_db", "test_table", "id", "timestamp", "", "1"
        )


def test_create_replacement_table_invalid_ddl_syntax(update_schemas):
    """Test replacement table creation with invalid DDL syntax."""
    update_schemas.table_exists = MagicMock(return_value=False)
    update_schemas.ch_client = MagicMock()
    update_schemas.ch_client.execute = MagicMock(side_effect=Exception("Syntax error"))

    with pytest.raises(Exception, match="An error occurred in creating replacement table: Database operation failed. Ensure schemas are deployed first using apply-schemas command before running updates. Check server logs for technical details if the issue persists."):
        update_schemas.create_replacement_table(
            "test_db", "test_table", "id", "timestamp", "", "1"
        )


def test_create_replacement_table_already_exists(update_schemas):
    """Test replacement table creation when table already exists."""
    update_schemas.table_exists = MagicMock(return_value=True)  # Replacement table already exists
    update_schemas.get_existing_schema_ddl = MagicMock(return_value="""
    CREATE TABLE test_db.test_table (
        id UInt32,
        timestamp DateTime
    ) ENGINE = MergeTree()
    PRIMARY KEY (id)
    ORDER BY (timestamp)
    """)
    update_schemas.ch_client = MagicMock()
    update_schemas.ch_client.execute = MagicMock()

    update_schemas.create_replacement_table(
        "test_db", "test_table", "id", "timestamp", "", "1"
    )

    assert update_schemas.ch_client.execute.call_count >= 2


def test_drop_replacement_table_not_exists(update_schemas):
    """Test dropping replacement table when it doesn't exist."""
    database_name = "test_db"
    table_name = "test_table"
    update_schemas.ch_client.execute = MagicMock()

    update_schemas.drop_replacement_table(database_name, table_name)

    expected_query = f"DROP TABLE IF EXISTS {database_name}.replacement_{table_name};"
    update_schemas.ch_client.execute.assert_called_once()
    actual_query = update_schemas.ch_client.execute.call_args[0][0].strip()
    assert actual_query == expected_query.strip()


def test_create_replacement_table_with_complex_ddl(update_schemas):
    """Test replacement table creation with complex DDL including projections."""
    update_schemas.table_exists = MagicMock(side_effect=[False, True])
    update_schemas.ch_client = MagicMock()
    update_schemas.ch_client.execute = MagicMock()

    update_schemas.get_existing_schema_ddl = MagicMock(return_value="""
    CREATE TABLE test_db.test_table (
        id UInt32,
        timestamp DateTime,
        data String,
        INDEX idx_data data TYPE bloom_filter GRANULARITY 1,
        PROJECTION proj_data (SELECT id, data WHERE length(data) > 10)
    ) ENGINE = MergeTree()
    PRIMARY KEY (id)
    ORDER BY (timestamp)
    """)

    update_schemas.create_replacement_table(
        "test_db", "test_table", "id", "timestamp", "", "1"
    )

    assert update_schemas.ch_client.execute.called
    create_calls = [call for call in update_schemas.ch_client.execute.call_args_list if 'CREATE TABLE' in str(call)]
    assert len(create_calls) > 0
    query = create_calls[-1][0][0]

    assert "AS test_db.test_table" in query
    assert "PARTITION BY toYYYYMMDD(timestamp_load)" in query
    assert "PRIMARY KEY (timestamp_load)" in query
    assert "ORDER BY (timestamp_load, timestamp)" in query


def test_create_replacement_table_concurrent_creation(update_schemas):
    """Test replacement table creation with concurrent access."""
    update_schemas.table_exists = MagicMock(side_effect=[False, True])  # Table created by another process
    update_schemas.ch_client = MagicMock()
    update_schemas.ch_client.execute = MagicMock()
    update_schemas.get_existing_schema_ddl = MagicMock(return_value="""
    CREATE TABLE test_db.test_table (
        id UInt32,
        timestamp DateTime
    ) ENGINE = MergeTree()
    PRIMARY KEY (id)
    ORDER BY (timestamp)
    """)

    update_schemas.create_replacement_table(
        "test_db", "test_table", "id", "timestamp", "", "1"
    )

    assert update_schemas.ch_client.execute.called
