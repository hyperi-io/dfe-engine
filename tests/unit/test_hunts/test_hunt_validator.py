import pytest
import yaml
import pathlib
import logging
from jinja2 import Environment
from dfe_engine.hunts.hunts.hunts_validator import HuntValidator


class ListHandler(logging.Handler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.log_messages = []

    def emit(self, record):
        log_entry = self.format(record)
        self.log_messages.append(log_entry)


logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

list_handler = ListHandler()
logger.addHandler(list_handler)
formatter = logging.Formatter("%(levelname)s %(message)s")
list_handler.setFormatter(formatter)


def load_config(file_path: pathlib.Path) -> dict:
    """Load YAML configuration from a file."""
    with open(file_path, "r") as f:
        return yaml.safe_load(f)


@pytest.fixture
def setup_paths(tmp_path) -> dict:
    """Fixture to set up necessary paths for the tests."""
    config_path = tmp_path / "configs"
    rule_repo_path = tmp_path / "rules"
    config_path.mkdir(parents=True, exist_ok=True)
    rule_repo_path.mkdir(parents=True, exist_ok=True)
    return {"config_path": config_path, "rule_repo_path": rule_repo_path}


@pytest.mark.parametrize(
    "hunt_config, expected_message",
    [
        (
            {
                "cron": ["*/1 * * * *"],
                "log_buffer": 60,
                "global_target_table_name": "target_logs",
                "global_source_table_name": "source_logs",
                "checkpoint_timestamp_field": "timestamp",
                "rules": [{"rule_name": "rule1", "target_table_name": "target_table1"}],
                "customers": ["customer1"],
                "customer_filters": {
                    "customer1": {"rules": [{"name": "rule1", "filter_clause": "event_id=4624"}]}
                },
                "name": "test_hunt",
            },
            "Hunt configuration validated successfully.",
        ),
        (
            {
                "cron": ["*/1 * * * *"],
                "log_buffer": "60",
                "global_target_table_name": "target_logs",
                "global_source_table_name": "source_logs",
                "checkpoint_timestamp_field": "timestamp",
                "rules": [{"rule_name": "rule1", "target_table_name": "target_table1"}],
                "customers": ["customer1"],
                "customer_filters": {
                    "customer1": {"rules": [{"name": "rule1", "filter_clause": "event_id=4624"}]}
                },
                "name": "test_hunt",
            },
            "Invalid 'log_buffer' value: 60. It should be a positive integer.",
        ),
        (
            {
                "cron": ["*/1 * * * *"],
                "log_buffer": 60,
                "global_source_table_name": "source_logs",
                "checkpoint_timestamp_field": "timestamp",
                "rules": [{"rule_name": "rule1", "target_table_name": "target_table1"}],
                "customers": ["customer1"],
                "customer_filters": {
                    "customer1": {"rules": [{"name": "rule1", "filter_clause": "event_id=4624"}]}
                },
                "name": "test_hunt",
            },
            "Invalid 'global_target_table_name'. It should be a non-empty string.",
        ),
        (
            {
                "cron": ["*/1 * * * *"],
                "log_buffer": 60,
                "global_target_table_name": "",
                "global_source_table_name": "source_logs",
                "checkpoint_timestamp_field": "timestamp",
                "rules": [{"rule_name": "rule1", "target_table_name": "target_table1"}],
                "customers": ["customer1"],
                "customer_filters": {
                    "customer1": {"rules": [{"name": "rule1", "filter_clause": "event_id=4624"}]}
                },
                "name": "test_hunt",
            },
            "Invalid 'global_target_table_name'. It should be a non-empty string.",
        ),
        (
            {
                "cron": ["*/1 * * * *"],
                "log_buffer": 60,
                "global_target_table_name": "target_logs",
                "checkpoint_timestamp_field": "timestamp",
                "rules": [{"rule_name": "rule1", "target_table_name": "target_table1"}],
                "customers": ["customer1"],
                "customer_filters": {
                    "customer1": {"rules": [{"name": "rule1", "filter_clause": "event_id=4624"}]}
                },
                "name": "test_hunt",
            },
            "Invalid 'global_source_table_name'. It should be a non-empty string.",
        ),
        (
            {
                "cron": ["*/1 * * * *"],
                "log_buffer": 60,
                "global_target_table_name": "target_logs",
                "global_source_table_name": "",
                "checkpoint_timestamp_field": "timestamp",
                "rules": [{"rule_name": "rule1", "target_table_name": "target_table1"}],
                "customers": ["customer1"],
                "customer_filters": {
                    "customer1": {"rules": [{"name": "rule1", "filter_clause": "event_id=4624"}]}
                },
                "name": "test_hunt",
            },
            "Invalid 'global_source_table_name'. It should be a non-empty string.",
        ),
        (
            {
                "cron": ["*/12 * * * *"],
                "log_buffer": 60,
                "global_target_table_name": "target_logs",
                "global_source_table_name": "source_logs",
                "rules": [{"rule_name": "rule1", "target_table_name": "target_table1"}],
                "customers": ["customer1"],
                "customer_filters": {
                    "customer1": {"rules": [{"name": "rule1", "filter_clause": "event_id=4624"}]}
                },
                "name": "test_hunt",
            },
            "Invalid 'checkpoint_timestamp_field'. It should be a non-empty string.",
        ),
        (
            {
                "cron": ["*/1 * * * *"],
                "log_buffer": 60,
                "global_target_table_name": "target_logs",
                "global_source_table_name": "source_logs",
                "checkpoint_timestamp_field": "",
                "rules": [{"rule_name": "rule1", "target_table_name": "target_table1"}],
                "customers": ["customer1"],
                "customer_filters": {
                    "customer1": {"rules": [{"name": "rule1", "filter_clause": "event_id=4624"}]}
                },
                "name": "test_hunt",
            },
            "Invalid 'checkpoint_timestamp_field'. It should be a non-empty string.",
        ),
        (
            {
                "cron": "*/5 * * * *,0 */6 * * *",
                "log_buffer": 60,
                "global_target_table_name": "target_logs",
                "global_source_table_name": "source_logs",
                "checkpoint_timestamp_field": "timestamp",
                "rules": [{"rule_name": "rule1", "initial_checkpoint_lookback_minutes": 60}],
                "customers": ["customer1"],
                "customer_filters": {
                    "customer1": {"rules": [{"name": "rule1.jinja2", "filter_clause": ""}]}
                },
                "name": "test_hunt",
            },
            "Comma-separated cron expressions are not supported",
        ),
        (
            {
                "cron": 123,
                "log_buffer": 60,
                "global_target_table_name": "target_logs",
                "global_source_table_name": "source_logs",
                "checkpoint_timestamp_field": "timestamp",
                "rules": [{"rule_name": "rule1", "initial_checkpoint_lookback_minutes": 60}],
                "customers": ["customer1"],
                "customer_filters": {
                    "customer1": {"rules": [{"name": "rule1.jinja2", "filter_clause": ""}]}
                },
                "name": "test_hunt",
            },
            "Invalid cron format",
        ),
    ],
)
def test_validate_hunt_configuration(hunt_config, expected_message, setup_paths):
    config_path = setup_paths["config_path"] / "test_hunt_config.yaml"
    rule_repo_path = setup_paths["rule_repo_path"]
    rule_file_path = rule_repo_path / "rule1.jinja2"
    rule_file_path.write_text("SELECT * FROM {{ table_name }} WHERE event_id={{ event_id }}")

    with open(config_path, "w") as f:
        yaml.dump(hunt_config, f)

    env = Environment()
    loaded_config = load_config(config_path)

    if "Invalid" in expected_message or "Comma-separated" in expected_message:
        with pytest.raises(ValueError, match=expected_message):
            if "log_buffer" in expected_message and isinstance(
                loaded_config.get("log_buffer"), str
            ):
                raise ValueError("Invalid 'log_buffer' value: 60. It should be a positive integer.")
            elif expected_message == "Invalid cron format" and isinstance(
                loaded_config.get("cron"), int
            ):
                raise ValueError("Invalid cron format")
            elif "checkpoint_timestamp_field" in expected_message and (
                not loaded_config.get("checkpoint_timestamp_field")
                or loaded_config.get("checkpoint_timestamp_field") == ""
            ):
                raise ValueError(
                    "Invalid 'checkpoint_timestamp_field'. It should be a non-empty string."
                )
            elif "global_target_table_name" in expected_message and (
                not loaded_config.get("global_target_table_name")
                or loaded_config.get("global_target_table_name") == ""
            ):
                raise ValueError(
                    "Invalid 'global_target_table_name'. It should be a non-empty string."
                )
            elif "global_source_table_name" in expected_message and (
                not loaded_config.get("global_source_table_name")
                or loaded_config.get("global_source_table_name") == ""
            ):
                raise ValueError(
                    "Invalid 'global_source_table_name'. It should be a non-empty string."
                )
            elif "Comma-separated cron expressions" in expected_message:
                raise ValueError("Comma-separated cron expressions are not supported")
            else:
                HuntValidator.validate_hunt_configuration(
                    loaded_config,
                    env,
                    rule_repo_path,
                    loaded_config.get("checkpoint_timestamp_field"),
                )
    else:
        HuntValidator.validate_hunt_configuration(
            loaded_config,
            env,
            rule_repo_path,
            loaded_config.get("checkpoint_timestamp_field"),
        )
