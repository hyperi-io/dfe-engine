import pytest
from dfe_engine.clickhouse.clickhouse_manager import ClickHouseManager

def test_get_instance_new(dfe_config_integration_target):
    clickhouse_manager = ClickHouseManager.get_instance(dfe_config_integration_target)
    assert(clickhouse_manager is not None)


def test_get_instance_existing(dfe_config_integration_target):
    clickhouse_manager = ClickHouseManager.get_instance(dfe_config_integration_target)
    new_clickhouse_manager = ClickHouseManager.get_instance(dfe_config_integration_target)
    assert(clickhouse_manager is new_clickhouse_manager)


def test_reset_instance(dfe_config_integration_target):
    clickhouse_manager = ClickHouseManager.get_instance(dfe_config_integration_target)
    ClickHouseManager.reset_instance()
    new_clickhouse_manager = ClickHouseManager.get_instance(dfe_config_integration_target)
    assert(clickhouse_manager is not new_clickhouse_manager)


def test_get_clickhouse_client(dfe_config_integration_target):
    clickhouse_manager = ClickHouseManager.get_instance(dfe_config_integration_target)
    ch_client = clickhouse_manager.get_clickhouse_client()
    assert(ch_client is not None)


def test_get_clickhouse_client_exception(dfe_config_integration_failing_target):
    clickhouse_manager = ClickHouseManager.get_instance(dfe_config_integration_failing_target)
    with pytest.raises(Exception) as exc_info:
        clickhouse_manager.get_clickhouse_client()
        assert("An unexpected error occurred during client acquistion: " in str(exc_info.value))


def test_get_clickhouse_client_after_reset(dfe_config_integration_failing_target):
    clickhouse_manager = ClickHouseManager.get_instance(dfe_config_integration_failing_target)
    with pytest.raises(Exception) as exc_info:
        clickhouse_manager.get_clickhouse_client()
        assert("An unexpected error occurred during client acquistion: " in str(exc_info.value))


def test_clickhouse_client_wrapper_execute(dfe_config_integration_target, test_clickhouse_client_wrapper_execute):
    query = test_clickhouse_client_wrapper_execute["query"]
    expected_result = test_clickhouse_client_wrapper_execute["expected_result"]

    clickhouse_manager = ClickHouseManager.get_instance(dfe_config_integration_target)
    ch_client = clickhouse_manager.get_clickhouse_client()

    result = ch_client.execute(query)
    assert(result == expected_result)


def test_clickhouse_client_wrapper_execute_all_operations(dfe_config_integration_target, clickhouse_environment_setup):
    clickhouse_manager = ClickHouseManager.get_instance(dfe_config_integration_target)
    ch_client = clickhouse_manager.get_clickhouse_client()

    database_name = clickhouse_environment_setup["database_name"]
    table_name = clickhouse_environment_setup["table_name"]

    # Insert test data into test_table
    ch_client.execute(f"INSERT INTO {database_name}.{table_name} (id, name) VALUES (1, 'Test1');")
    result = ch_client.execute(f"SELECT * FROM {database_name}.{table_name} ORDER BY id;")
    assert(result == [(1, "Test1")])
    ch_client.execute(f"INSERT INTO {database_name}.{table_name} (id, name) VALUES (2, 'Test2'), (3, 'Test3');")
    result = ch_client.execute(f"SELECT * FROM {database_name}.{table_name} ORDER BY id;")
    assert(result == [(1, "Test1"), (2, "Test2"), (3, "Test3")])
    ch_client.execute(f"INSERT INTO {database_name}.{table_name} (id, name) VALUES", [(4, "Test4"), (5, "Test5"), (6, "Test6")])
    result = ch_client.execute(f"SELECT * FROM {database_name}.{table_name} ORDER BY id;")
    assert(result == [(1, "Test1"), (2, "Test2"), (3, "Test3"), (4, "Test4"), (5, "Test5"), (6, "Test6")])

    # Alter (update) row of test data in test_table
    ch_client.execute(f"ALTER TABLE {database_name}.{table_name} UPDATE name = 'Updated_Test1' WHERE id = 1 SETTINGS mutations_sync = 1;")
    result = ch_client.execute(f"SELECT * FROM {database_name}.{table_name} ORDER BY id;")
    assert(result == [(1, "Updated_Test1"), (2, "Test2"), (3, "Test3"), (4, "Test4"), (5, "Test5"), (6, "Test6")])

    # Delete row of test data in test_table
    ch_client.execute(f"DELETE FROM {database_name}.{table_name} WHERE id = 1;")
    result = ch_client.execute(f"SELECT * FROM {database_name}.{table_name} ORDER BY id;")
    assert(result == [(2, "Test2"), (3, "Test3"), (4, "Test4"), (5, "Test5"), (6, "Test6")])
    
    # Set session settings
    ch_client.execute("SET max_memory_usage = 1000000000;")
    result = ch_client.execute("SELECT name, value FROM system.settings WHERE name = 'max_memory_usage';")
    assert(result == [("max_memory_usage", "1000000000")])

    # Use test_db as session database
    ch_client.execute(f"USE {database_name};")
    result = ch_client.execute("SELECT database();")
    assert(result == [(f"{database_name}",)])

    # Alter (add column) to test_table
    ch_client.execute(f"ALTER TABLE {database_name}.{table_name} ADD COLUMN timestamp DateTime DEFAULT now();")
    result = ch_client.execute(f"DESCRIBE TABLE {database_name}.{table_name};")
    assert(result == [("id", "UInt32", "", "", "", "", ""), ("name", "String", "", "", "", "", ""), ("timestamp", "DateTime", "DEFAULT", "now()", "", "", "")])

    # Alter (modify column) to test_table
    ch_client.execute(f"ALTER TABLE {database_name}.{table_name} MODIFY COLUMN timestamp DateTime DEFAULT '1970-01-01T00:00:00';")
    result = ch_client.execute(f"DESCRIBE TABLE {database_name}.{table_name};")
    assert(result == [("id", "UInt32", "", "", "", "", ""), ("name", "String", "", "", "", "", ""), ("timestamp", "DateTime", "DEFAULT", "'1970-01-01T00:00:00'", "", "", "")])

    # Alter (drop column) from test_table
    ch_client.execute(f"ALTER TABLE {database_name}.{table_name} DROP COLUMN timestamp;")
    result = ch_client.execute(f"DESCRIBE TABLE {database_name}.{table_name};")
    assert(result == [("id", "UInt32", "", "", "", "", ""), ("name", "String", "", "", "", "", "")])

    # Optimize test_table
    ch_client.execute(f"OPTIMIZE TABLE {database_name}.{table_name} FINAL;")
    result = ch_client.execute(f"SELECT count() FROM system.merges WHERE database = '{database_name}' AND table = '{table_name}';")
    assert(result == [(0,)])

    # Check test_table
    result = ch_client.execute(f"CHECK TABLE {database_name}.{table_name};")
    assert(result == 1)

    # Rename test_table to test_table_renamed
    ch_client.execute(f"RENAME TABLE {database_name}.{table_name} TO {database_name}.{table_name}_renamed;")
    result = ch_client.execute(f"SELECT 1 FROM system.tables WHERE database = '{database_name}' AND name = '{table_name}';")
    assert(result == [])
    result = ch_client.execute(f"SELECT 1 FROM system.tables WHERE database = '{database_name}' AND name = '{table_name}_renamed';")
    assert(result == [(1,)])

    # Rename test_table_renamed back to test_table
    ch_client.execute(f"RENAME TABLE {database_name}.{table_name}_renamed TO {database_name}.{table_name};")
    result = ch_client.execute(f"SELECT 1 FROM system.tables WHERE database = '{database_name}' AND name = '{table_name}';")
    assert(result == [(1,)])
    result = ch_client.execute(f"SELECT 1 FROM system.tables WHERE database = '{database_name}' AND name = '{table_name}_renamed';")
    assert(result == [])

    # Detach test_table
    ch_client.execute(f"DETACH TABLE {database_name}.{table_name};")
    result = ch_client.execute(f"SELECT 1 FROM system.tables WHERE database = '{database_name}' AND name = '{table_name}';")
    assert(result == [])

    # Re-attach test_table
    ch_client.execute(f"ATTACH TABLE {database_name}.{table_name};")
    result = ch_client.execute(f"SELECT 1 FROM system.tables WHERE database = '{database_name}' AND name = '{table_name}';")
    assert(result == [(1,)])

    # Exchange table with new dummy table
    ch_client.execute(f"CREATE TABLE IF NOT EXISTS {database_name}.{table_name}_dummy (id UInt32, name String) ENGINE = MergeTree() ORDER BY id;")
    result = ch_client.execute(f"SELECT 1 FROM system.tables WHERE database = '{database_name}' AND name = '{table_name}_dummy';")
    assert(result == [(1,)])
    ch_client.execute(f"EXCHANGE TABLES {database_name}.{table_name} AND {database_name}.{table_name}_dummy;")
    result = ch_client.execute(f"SELECT * FROM {database_name}.{table_name} ORDER BY id;")
    assert(result == [])
    result = ch_client.execute(f"SELECT * FROM {database_name}.{table_name}_dummy ORDER BY id;")
    assert(result == [(2, "Test2"), (3, "Test3"), (4, "Test4"), (5, "Test5"), (6, "Test6")])

    # Exchange table back with original table
    ch_client.execute(f"EXCHANGE TABLES {database_name}.{table_name}_dummy AND {database_name}.{table_name};")
    result = ch_client.execute(f"SELECT * FROM {database_name}.{table_name} ORDER BY id;")
    assert(result == [(2, "Test2"), (3, "Test3"), (4, "Test4"), (5, "Test5"), (6, "Test6")])
    result = ch_client.execute(f"SELECT * FROM {database_name}.{table_name}_dummy ORDER BY id;")
    assert(result == [])

    # Drop dummy table
    ch_client.execute(f"DROP TABLE {database_name}.{table_name}_dummy;")
    result = ch_client.execute(f"SELECT 1 FROM system.tables WHERE database = '{database_name}' AND name = '{table_name}_dummy';")
    assert(result == [])

    # System commands (can only be verified by no exceptions raised or querying system.query_log)
    ch_client.execute("SYSTEM FLUSH LOGS;")
    ch_client.execute("SYSTEM DROP MARK CACHE;")

    # Kill random query (can only be verified by no exceptions raised or querying system.query_log)
    ch_client.execute("KILL QUERY WHERE query_id = 'test_query_id';")


def test_clickhouse_client_wrapper_execute_bad_insert(dfe_config_integration_target, clickhouse_environment_setup):
    clickhouse_manager = ClickHouseManager.get_instance(dfe_config_integration_target)
    ch_client = clickhouse_manager.get_clickhouse_client()

    database_name = clickhouse_environment_setup["database_name"]
    table_name = clickhouse_environment_setup["table_name"]
    
    # Try to insert test data into test_table with bad syntax
    with pytest.raises(ValueError) as exc_info:
        ch_client.execute(f"INSERT INTO {database_name}.{table_name} (id, name)", [(1, "Test1")])
    assert(f"Cannot parse INSERT query for data insertion: INSERT INTO {database_name}.{table_name} (id, name)..." == str(exc_info.value))


def test_teardown_test_databases(dfe_config_integration_target, clickhouse_environment_setup):
    clickhouse_manager = ClickHouseManager.get_instance(dfe_config_integration_target)
    ch_client = clickhouse_manager.get_clickhouse_client()

    database_name = clickhouse_environment_setup["database_name"]

    clickhouse_manager.teardown_test_databases([database_name])
    result = ch_client.execute(f"SELECT 1 FROM system.databases WHERE name = '{database_name}';")
    assert(result == [])