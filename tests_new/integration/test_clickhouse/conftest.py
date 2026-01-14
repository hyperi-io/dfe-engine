import pytest
from datetime import datetime
from dfe_engine.clickhouse.clickhouse_manager import ClickHouseManager

@pytest.fixture
def dfe_config():
    return {
        "default_target": "integration",
        "targets": {
            "integration": {
                "ch_host": "localhost",
                "ch_port": 8124,
                "ch_username": "default",
                "ch_password": "",
                "ch_secure": False,
                "helm_template": "resources/pipeline_template/config_only.yaml",
                "hunt_config_path": "tests/resources/workshop_setup/test_hunts",
                "hunt_rules_path": "tests/resources/workshop_setup/test_rules/executables",
                "ip_config_bucket_name": "dfe-test-config-bucket",
                "ip_config_bucket_region": "ap-southeast-2",
                "ip_config_geo_ip_path": "geoip",
                "ip_config_receiver_path": "standard_enrichment_files",
                "ip_config_standard_enrichment_path": "standard_enrichment_files",
                "ip_templates_path": "vector_templates",
                "vector_config_mount_path": "/etc/vector"
            },
            "integration_failing": {
                "ch_host": "localhost",
                "ch_port": 8124,
                "ch_username": "default",
                "ch_password": "",
                "ch_secure": True,
                "helm_template": "resources/pipeline_template/config_only.yaml",
                "hunt_config_path": "tests/resources/workshop_setup/test_hunts",
                "hunt_rules_path": "tests/resources/workshop_setup/test_rules/executables",
                "ip_config_bucket_name": "dfe-test-config-bucket",
                "ip_config_bucket_region": "ap-southeast-2",
                "ip_config_geo_ip_path": "geoip",
                "ip_config_receiver_path": "standard_enrichment_files",
                "ip_config_standard_enrichment_path": "standard_enrichment_files",
                "ip_templates_path": "vector_templates",
                "vector_config_mount_path": "/etc/vector"
            }
        }
    }


@pytest.fixture
def dfe_config_integration_target(dfe_config):
    return dfe_config["targets"]["integration"]


@pytest.fixture
def dfe_config_integration_failing_target(dfe_config):
    return dfe_config["targets"]["integration_failing"]


@pytest.fixture
def date_time_anchor():
    return datetime.now().strftime("%Y%m%d_%H%M%S")


@pytest.fixture
def clickhouse_environment_setup(date_time_anchor, dfe_config_integration_target):
    clickhouse_manager = ClickHouseManager.get_instance(dfe_config_integration_target)
    ch_client = clickhouse_manager.get_clickhouse_client()

    database_name = f"test_db_{date_time_anchor}"
    user_name = f"test_user_{date_time_anchor}"
    table_name = f"test_table_{date_time_anchor}"

    # Create test_database
    ch_client.execute(f"CREATE DATABASE IF NOT EXISTS {database_name};")
    result = ch_client.execute(f"SELECT 1 FROM system.databases WHERE name = '{database_name}';")
    assert(result == [(1,)])

    # Create test_table
    ch_client.execute(f"CREATE TABLE IF NOT EXISTS {database_name}.{table_name} (id UInt32, name String) ENGINE = MergeTree() ORDER BY id;")
    result = ch_client.execute(f"SELECT 1 FROM system.tables WHERE database = '{database_name}' AND name = '{table_name}';")
    assert(result == [(1,)])
    
    # Create test_user
    ch_client.execute(f"CREATE USER IF NOT EXISTS {user_name} IDENTIFIED BY 'password123';")
    result = ch_client.execute(f"SELECT 1 FROM system.users WHERE name = '{user_name}';")
    assert(result == [(1,)])

    # Grant test_user access to all of the databases
    ch_client.execute(f"GRANT ALL ON {database_name}.* TO {user_name};")
    result = ch_client.execute(f"SHOW GRANTS FOR {user_name};")
    assert(result == [(f"GRANT CHECK, SHOW, SELECT, INSERT, ALTER, CREATE DATABASE, CREATE TABLE, CREATE VIEW, CREATE DICTIONARY, DROP DATABASE, DROP TABLE, DROP VIEW, DROP DICTIONARY, UNDROP TABLE, TRUNCATE, OPTIMIZE, BACKUP, CREATE ROW POLICY, ALTER ROW POLICY, DROP ROW POLICY, SHOW ROW POLICIES, SYSTEM MERGES, SYSTEM TTL MERGES, SYSTEM FETCHES, SYSTEM MOVES, SYSTEM PULLING REPLICATION LOG, SYSTEM CLEANUP, SYSTEM VIEWS, SYSTEM SENDS, SYSTEM REPLICATION QUEUES, SYSTEM VIRTUAL PARTS UPDATE, SYSTEM REDUCE BLOCKING PARTS, SYSTEM DROP REPLICA, SYSTEM SYNC REPLICA, SYSTEM RESTART REPLICA, SYSTEM RESTORE REPLICA, SYSTEM RESTORE DATABASE REPLICA, SYSTEM WAIT LOADING PARTS, SYSTEM SYNC DATABASE REPLICA, SYSTEM FLUSH DISTRIBUTED, SYSTEM LOAD PRIMARY KEY, SYSTEM UNLOAD PRIMARY KEY, dictGet ON {database_name}.* TO {user_name}",)])

    # Return test environment details
    yield {
        "database_name": database_name,
        "table_name": table_name,
        "user_name": user_name
    }

    # Revoke test_user access from all of the databases
    ch_client.execute(f"REVOKE ALL ON {database_name}.* FROM {user_name};")
    result = ch_client.execute(f"SHOW GRANTS FOR {user_name};")
    assert(result == [])

    # Truncate test data from test_table
    ch_client.execute(f"TRUNCATE TABLE IF EXISTS {database_name}.{table_name};")
    result = ch_client.execute(f"EXISTS {database_name}.{table_name};")
    if result == [(1,)]:
        result = ch_client.execute(f"SELECT * FROM {database_name}.{table_name} ORDER BY id;")
        assert(result == [])

    # Drop test_user
    ch_client.execute(f"DROP USER IF EXISTS {user_name};")
    result = ch_client.execute(f"SELECT 1 FROM system.users WHERE name = '{user_name}';")
    assert(result == [])

    # Drop test_table
    ch_client.execute(f"DROP TABLE IF EXISTS {database_name}.{table_name};")
    result = ch_client.execute(f"SELECT 1 FROM system.tables WHERE database = '{database_name}' AND name = '{table_name}';")
    assert(result == [])

    # Drop test_database
    ch_client.execute(f"DROP DATABASE IF EXISTS {database_name};")
    result = ch_client.execute(f"SELECT 1 FROM system.databases WHERE name = '{database_name}';")
    assert(result == [])


@pytest.fixture(autouse = True)
def reset_clickhouse_singleton():
    ClickHouseManager.reset_instance()
    yield
    ClickHouseManager.reset_instance()