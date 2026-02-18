import os
import pytest
import yaml
import uuid
import stat
import shutil

from hyperi_pylib.logger import logger
from dfe_engine.settings import get_settings, reset_settings
from dfe_engine.clickhouse.clickhouse_manager import ClickHouseManager

# Reset settings and ClickHouseManager to pick up dotenv values
reset_settings()
ClickHouseManager.reset_instance()


@pytest.fixture(scope="session")
def dfe_config_fixtures(tmp_path_factory):
    """Create test configuration with proper paths."""
    reset_settings()
    settings = get_settings()

    tmp_path_factory.mktemp("test_dfe_schema_output")
    targets_dir = tmp_path_factory.mktemp("test_targets")
    targets_path = targets_dir / "dfe_targets.yaml"

    targets_data = {
        "default_target": "test_schemas",
        "targets": {
            "test_schemas": {
                "ch_host": settings.clickhouse.host,
                "ch_port": settings.clickhouse.port,
                "ch_username": settings.clickhouse.username,
                "ch_password": settings.clickhouse.password,
                "ch_database": settings.clickhouse.database,
                "ch_secure": settings.clickhouse.secure,
                "ch_verify": settings.clickhouse.verify,
                "hunt_config_path": "tests/tmp/test_hunts",
                "hunt_rules_path": "tests/tmp/test_rules/executables",
            }
        },
    }

    with open(targets_path, "w") as f:
        yaml.dump(targets_data, f)

    config_data = {
        "global_settings": {
            "hunt_checkpoint_path": "tests/tmp/.dfe_hunt_checkpoint",
            "default_target": "test_schemas",
            "target_path": str(targets_path),
            "schema_common_version": "v001.001.007",
            "use_json_feature": False,
            "derived_schema_paths": "tests/resources/test_schemas/stable_schemas",
            "schema_output_path": "tests/job_outputs/test_schema_output",
        },
        "organisations": [
            {
                "cluster_name": "",
                "org_id": "rule_compilation_04212734",
                "schemas": ["logs_alerts", "logs_omss_aws_guardduty"],
            }
        ],
        "build_schemas": {
            "no_cluster_declarations_needed": True,
            "use_replicated_merge_tree": False,
            "use_shared_merge_tree": False,
        },
        "apply_schemas": {
            "do_add_roles": False,
            "do_add_columns": True,
        },
        "schemas": {
            "logs_alerts": {
                "name": "logs_alerts",
                "meta_schema": "logs_alerts.csv",
                "meta_schema_version": "v001.000.000",
                "derived_schema_file_path": "logs_alerts/logs_alerts_sub.csv",
                "additional_fields_config": "logs_alerts/logs_alerts_add.csv",
            },
            "logs_omss_aws_guardduty": {
                "name": "logs_omss_aws_guardduty",
                "meta_schema": "logs_base.csv",
                "meta_schema_version": "v001.000.000",
                "derived_schema_file_path": None,
                "additional_fields_config": "logs_omss_aws_guardduty/logs_omss_aws_guardduty_add.csv",
                "derived_schema_ttl": 7,
            },
        },
        "hunt_scheduler": {
            "checkpoint_destination": "clickhouse",
            "num_threads": 8,
            "hunt_dir": "tests/tmp/test_hunts",
            "rule_repo_dir": "tests/tmp/test_rules/executables",
            "hunt_config_path": "tests/tmp/test_hunts",
            "hunt_rules_path": "tests/tmp/test_rules/executables",
        },
    }

    return config_data


@pytest.fixture(scope="session")
def setup_paths():
    dfe_root_log_path = os.path.join(os.getcwd(), "tmp", "logs_path")
    os.makedirs(dfe_root_log_path, exist_ok=True)
    hunt_log_path = os.path.join(os.getcwd(), "tmp", "hunt_path")
    os.makedirs(hunt_log_path, exist_ok=True)

    hunt_config_path = os.path.join(os.getcwd(), "tests/tmp/test_hunts")
    os.makedirs(hunt_config_path, exist_ok=True)
    hunt_rules_path = os.path.join(os.getcwd(), "tests/tmp/test_rules/executables")
    os.makedirs(hunt_rules_path, exist_ok=True)
    hunt_checkpoint_path = os.path.join(os.getcwd(), "tests/tmp/.dfe_hunt_checkpoint")
    os.makedirs(hunt_checkpoint_path, exist_ok=True)

    return (
        dfe_root_log_path,
        hunt_log_path,
        hunt_config_path,
        hunt_rules_path,
        hunt_checkpoint_path,
    )


@pytest.fixture(scope="session")
def unique_names():
    unique_id = str(uuid.uuid4()).replace("-", "")
    database_name = f"dfe_audit_{unique_id}"
    table_name = f"detection_checkpoint_{unique_id}"
    return database_name, table_name


@pytest.fixture(scope="session")
def dfe_package(dfe_config_fixtures, setup_paths):
    dfe_package_path = os.path.join(os.getcwd(), "dfe_package_testing.yaml")

    with open(dfe_package_path, "w") as f:
        yaml.dump(dfe_config_fixtures, f)

    hunt_config = {
        "name": "AWS GuardDuty Hunt Config",
        "cron": ["* * * * *"],
        "log_buffer": 60,
        "global_target_table_name": "logs_alerts",
        "global_source_table_name": "logs_omss_aws_guardduty",
        "checkpoint_timestamp_field": "timestamp_load",
        "customers": ["rule_compilation_04212734"],
        "rules": [
            {
                "rule_name": "aws_guardduty_pentest",
                "initial_checkpoint_lookback_minutes": 120,
            }
        ],
        "customer_filters": {
            "rule_compilation_04212734": {
                "rules": [{"name": "aws_guardduty_pentest.jinja2", "filter_clause": ""}]
            }
        },
    }

    hunt_config_path = os.path.join(setup_paths[2], "test_hunt_config.yaml")
    with open(hunt_config_path, "w") as f:
        yaml.dump(hunt_config, f)

    executable_rule = """
    INSERT INTO {{org_id}}.{{target_table_name}}
    (
        alert_description, alert_framework, alert_ratingtime_sla_applies, alert_rule_name, alert_schedule,
        alert_schedule_duration, alert_severity, alert_triage_score, alert_type, detected_time, logoriginal,
        org_id, source_table, tactic_id, tactic_name, tactic_reference, timestamp, event_record_type,
        event_severity, cloud_account_id, event_info, event_name, event_outcome, event_status,
        source_ip, source_hostname, destination_ip, destination_hostname
    )
    SELECT
        'Detects penetration testing related attacks', 'MITRE ATT&CK', 'true', 'aws_guardduty_pentest', 'smd',
        '1m', 'medium', 40, 'Detection of Penetration Testing Attack', NOW(), logoriginal, '{{org_id}}',
        'logs_omss_aws_guardduty', 'TA0006, TA0005', 'Credential Access, Defense Evasion',
        'https://attack.mitre.org/tactics/TA0006, https://attack.mitre.org/tactics/TA0005', timestamp_load,
        event_record_type, event_severity, cloud_account_id, event_info, event_name, event_outcome, event_status,
        source_ip, source_hostname, destination_ip, destination_hostname
    FROM
        {{org_id}}.logs_omss_aws_guardduty
    WHERE
        event_record_type REGEXP 'PenTest.*'
        AND event_severity IN ('1', '2', '3')
    """

    executable_rule_path = os.path.join(setup_paths[3], "aws_guardduty_pentest.jinja2")
    with open(executable_rule_path, "w") as f:
        f.write(executable_rule)

    return dfe_package_path


test_stats = {"total": 0, "passed": 0, "failed": 0, "skipped": 0, "test_files": {}}


def pytest_runtest_logreport(report):
    """Collect test statistics."""
    if report.when == "call":
        test_stats["total"] += 1

        test_file = report.nodeid.split("::")[0]
        if test_file not in test_stats["test_files"]:
            test_stats["test_files"][test_file] = {
                "total": 0,
                "passed": 0,
                "failed": 0,
                "skipped": 0,
            }

        if report.passed:
            test_stats["passed"] += 1
            test_stats["test_files"][test_file]["passed"] += 1
        elif report.failed:
            test_stats["failed"] += 1
            test_stats["test_files"][test_file]["failed"] += 1
        elif report.skipped:
            test_stats["skipped"] += 1
            test_stats["test_files"][test_file]["skipped"] += 1

        test_stats["test_files"][test_file]["total"] += 1


def pytest_terminal_summary(terminalreporter, exitstatus):
    """Print test summary at the end."""
    logger.info("\n===================== test_schemas Test Summary =====================")
    logger.info("Overall Statistics:")
    logger.info(f"Total Tests Run: {test_stats['total']}")
    logger.info(f"Tests Passed:    {test_stats['passed']}")
    logger.info(f"Tests Failed:    {test_stats['failed']}")
    logger.info(f"Tests Skipped:   {test_stats['skipped']}")
    logger.info("\nBreakdown by Test File:")

    for test_file, stats in test_stats["test_files"].items():
        logger.info(f"\n{test_file}:")
        logger.info(f"  Total Tests: {stats['total']}")
        logger.info(f"  Passed:      {stats['passed']}")
        logger.info(f"  Failed:      {stats['failed']}")
        logger.info(f"  Skipped:     {stats['skipped']}")

    logger.info("=================================================================")


@pytest.fixture
def target_config_data():
    """Mock target config data for unit tests."""
    return {"ch_host": "localhost", "ch_port": 8123}


@pytest.fixture(scope="session", autouse=True)
def cleanup(tmp_path_factory):
    """Fixture to clean up any residual files or directories created during tests."""
    temp_dir = tmp_path_factory.mktemp("test_dfe_data_engine")
    yield
    if os.path.exists(temp_dir):

        def on_rm_error(func, path, exc_info):
            os.chmod(path, stat.S_IWRITE)
            func(path)

        shutil.rmtree(temp_dir, onerror=on_rm_error)
