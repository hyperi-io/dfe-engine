import pytest
from unittest.mock import MagicMock, patch
from jinja2 import Environment, FileSystemLoader
from dfecli.dfe_async_hunts.hunts.hunts import Hunt
import yaml
import logging
from pathlib import Path
import shutil
import tempfile
import os


def load_config(file_path: Path) -> dict:
    with open(file_path, "r") as f:
        return yaml.safe_load(f)


@pytest.fixture(scope="session")
def dfe_logger():
    log_path = os.path.join(
        os.getcwd(), "tests/test_logs/test_hunt_win_alert_hacktool_ruler"
    )
    if not os.path.exists(log_path):
        os.makedirs(log_path)

    log_file = os.path.join(log_path, "test_hunt_win_alert_hacktool_ruler.log")

    dfe_logger = logging.getLogger("dfe_logger")
    dfe_logger.setLevel(logging.INFO)

    file_handler = logging.FileHandler(log_file)
    file_handler.setLevel(logging.INFO)

    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    file_handler.setFormatter(formatter)

    if not dfe_logger.hasHandlers():
        dfe_logger.addHandler(file_handler)

    yield dfe_logger


@pytest.fixture(scope="session")
def template_dir():
    with tempfile.TemporaryDirectory() as tmpdirname:
        template_path = Path(tmpdirname)

        rule_names = ["rule1", "rule2"]

        for rule_name in rule_names:
            rule_template_path = template_path / f"{rule_name}.jinja2"
            with open(rule_template_path, "w") as f:
                f.write(
                    """
                INSERT INTO {{ org_id }}.{{ target_table_name }} (alert_description, timestamp, event_original, org_id, source_table)
                SELECT 'Possible Malicious Hacking Tools', NOW(), event_original, '{{ org_id }}', '{{ source_table_name }}'
                FROM {{ org_id }}.{{ source_table_name }} WHERE event_record_type IN ('4776', '4624', '4625')
                """.replace("\n", " ")
                    .replace("\t", " ")
                    .strip()
                )

        yield template_path


@pytest.fixture(scope="session")
def file_and_folder_paths(tmp_path_factory):
    Path(__file__).resolve().parent.parent
    output_test_dir = tmp_path_factory.mktemp("output_test_dir")

    paths = {
        "dfe_output_path": output_test_dir,
        "tmp/logs": tmp_path_factory.mktemp("logs_path"),
        "hunt_log_path": tmp_path_factory.mktemp("hunt_path"),
        "hunt_checkpoint_path": tmp_path_factory.mktemp("dfe_hunt_checkpoint"),
    }

    yield paths

    for path in paths.values():
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def hunt_instance(request, file_and_folder_paths, template_dir, dfe_logger):
    MagicMock()

    env = Environment(loader=FileSystemLoader(template_dir))
    hunt_data = request.param

    hunt_data["hunt_log_path"] = str(file_and_folder_paths["hunt_log_path"])
    hunt_data["hunt_checkpoint_path"] = str(
        file_and_folder_paths["hunt_checkpoint_path"]
    )

    hunt = Hunt(**hunt_data, dfe_logger=dfe_logger)
    return hunt, env, dfe_logger


@pytest.fixture
def hunt_inputs(request, file_and_folder_paths, template_dir, dfe_logger):
    env = Environment(loader=FileSystemLoader(template_dir))
    hunt_data = request.param

    hunt_data["hunt_log_path"] = str(file_and_folder_paths["hunt_log_path"])
    hunt_data["hunt_checkpoint_path"] = str(
        file_and_folder_paths["hunt_checkpoint_path"]
    )

    return hunt_data, env, dfe_logger


@pytest.mark.parametrize(
    "hunt_inputs, org_id, expected_in_query, expected_exception",
    [
        (
            {
                "cron": "* * * * *",
                "log_buffer": 60,
                "customer": "customer1",
                "rules": [{"rule_name": "rule1"}, {"rule_name": "rule2"}],
                "name": "Test Hunt",
                "target_config_data": {},
                "checkpoint_destination": "clickhouse",
            },
            "customer1",
            "FROM customer1.windows_audit",
            TypeError,
        ),
        (
            {
                "cron": "* * * * *",
                "log_buffer": 60,
                "customer": "customer1",
                "rules": [{"rule_name": "rule1"}, {"rule_name": "rule2"}],
                "name": "Test Hunt",
                "target_config_data": {},
                "checkpoint_destination": "clickhouse",
            },
            "customer2",
            "FROM customer2.windows_audit",
            TypeError,
        ),
    ],
    indirect=["hunt_inputs"],
)
def test_sql_query_building_without_table_settings(
    dfe_logger, hunt_inputs, org_id, expected_in_query, expected_exception
):
    hunt_data, env, _ = hunt_inputs

    with pytest.raises(expected_exception):
        hunt = Hunt(**hunt_data, dfe_logger=dfe_logger)

        expected_render_output = f"SELECT * FROM {org_id}.windows_audit"

        with patch.object(Environment, "get_template") as mock_get_template:
            mock_template = MagicMock()
            mock_template.render.return_value = expected_render_output
            mock_get_template.return_value = mock_template

            result_queries = hunt.convert_yaml_to_sql(
                env, org_id, hunt.customer_filters
            )

            for query in result_queries:
                assert expected_in_query in query


@pytest.mark.parametrize(
    "hunt_instance, org_id, expected_target_sql_statement, expected_source_sql_statement",
    [
        (
            {
                "cron": "* * * * *",
                "log_buffer": 60,
                "customer": "customer1",
                "rules": [
                    {
                        "rule_name": "rule1",
                        "target_table_name": "logs_alerts",
                        "source_table_name": "logs_nxlog_windows",
                    }
                ],
                "name": "Test Hunt",
                "hunt_log_path": "",
                "global_target_table_name": "global_logs_alerts",
                "global_source_table_name": "global_logs_nxlog_windows",
                "checkpoint_timestamp_field": "timestamp",
                "target_config_data": {},
                "checkpoint_destination": "clickhouse",
                "hunt_checkpoint_path": "",
            },
            "customer1",
            "INSERT INTO customer1.logs_alerts",
            "FROM customer1.logs_nxlog_windows WHERE event_record_type",
        )
    ],
    indirect=["hunt_instance"],
)
def test_sql_query_building_with_local_table_settings(
    hunt_instance, org_id, expected_target_sql_statement, expected_source_sql_statement
):
    hunt, env, _ = hunt_instance
    result_queries = hunt.convert_yaml_to_sql(env, org_id, hunt.customer_filters)

    for query in result_queries:
        assert expected_target_sql_statement in query, (
            f"Expected '{expected_target_sql_statement}' not found in SQL query. "
            f"Actual query: '{query}'"
        )
        assert expected_source_sql_statement in query, (
            f"Expected '{expected_source_sql_statement}' not found in SQL query. "
            f"Actual query: '{query}'"
        )


@pytest.mark.parametrize(
    "hunt_instance, org_id, expected_target_sql_statement, expected_source_sql_statement",
    [
        (
            {
                "cron": "* * * * *",
                "log_buffer": 60,
                "customer": "customer1",
                "rules": [{"rule_name": "rule1"}, {"rule_name": "rule2"}],
                "name": "Test Hunt",
                "hunt_log_path": "path/to/logs",
                "global_target_table_name": "logs_alerts",
                "global_source_table_name": "logs_nxlog_windows",
                "checkpoint_timestamp_field": "timestamp",
                "target_config_data": {},
                "checkpoint_destination": "clickhouse",
                "hunt_checkpoint_path": "path/to/checkpoints",
            },
            "customer1",
            "INSERT INTO customer1.logs_alerts",
            "FROM customer1.logs_nxlog_windows WHERE event_record_type",
        )
    ],
    indirect=["hunt_instance"],
)
def test_sql_query_building_with_global_table_settings(
    hunt_instance, org_id, expected_target_sql_statement, expected_source_sql_statement
):
    hunt, env, _ = hunt_instance
    result_queries = hunt.convert_yaml_to_sql(env, org_id, hunt.customer_filters)

    for query in result_queries:
        assert expected_target_sql_statement in query, (
            f"Expected '{expected_target_sql_statement}' not found in SQL query. "
            f"Actual query: '{query}'"
        )
        assert expected_source_sql_statement in query, (
            f"Expected '{expected_source_sql_statement}' not found in SQL query. "
            f"Actual query: '{query}'"
        )


@pytest.mark.parametrize(
    "hunt_instance, org_id, expected_target_sql_statement, expected_source_sql_statement",
    [
        (
            {
                "cron": "* * * * *",
                "log_buffer": 60,
                "customer": "customer2",
                "rules": [
                    {
                        "rule_name": "rule1",
                        "target_table_name": "logs_alerts",
                        "source_table_name": "logs_nxlog_windows",
                    },
                    {
                        "rule_name": "rule2",
                        "target_table_name": "logs_alerts",
                        "source_table_name": "logs_nxlog_windows",
                    },
                ],
                "name": "Test Hunt",
                "hunt_log_path": "path/to/logs",
                "global_target_table_name": "logs_alerts",
                "global_source_table_name": "logs_nxlog_windows",
                "checkpoint_timestamp_field": "timestamp",
                "target_config_data": {},
                "checkpoint_destination": "clickhouse",
                "hunt_checkpoint_path": "path/to/checkpoints",
            },
            "customer2",
            "INSERT INTO customer2.logs_alerts",
            "FROM customer2.logs_nxlog_windows WHERE event_record_type",
        )
    ],
    indirect=["hunt_instance"],
)
def test_sql_query_building_with_global_and_local_table_settings(
    hunt_instance, org_id, expected_target_sql_statement, expected_source_sql_statement
):
    hunt, env, _ = hunt_instance
    result_queries = hunt.convert_yaml_to_sql(env, org_id, hunt.customer_filters)

    for query in result_queries:
        assert expected_target_sql_statement in query, (
            f"Expected '{expected_target_sql_statement}' not found in SQL query. "
            f"Actual query: '{query}'"
        )
        assert expected_source_sql_statement in query, (
            f"Expected '{expected_source_sql_statement}' not found in SQL query. "
            f"Actual query: '{query}'"
        )
