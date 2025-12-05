from dfecli.dfe_async_hunts.hunts.hunts import Hunt
from dfecli.dfe_clickhouse.clickhouse_manager import ClickHouseManager
from dfecli.dfe_config.config_loader import DFEConfigLoader
from datetime import datetime
from dfecli.dfe_logger.dfe_logger import DFELog
from dfecli.dfe_elastic_watcher_converter.watcher_converter import WatcherConverter
from dfecli.dfe_elastic_watcher_converter.watcher_parser import WatcherParser
from jinja2 import Environment, FileSystemLoader
from pathlib import Path
from dfecli.dfe_schemabuilder.schema_controller import SchemaController
from datetime import timezone
import json
import logging
import os
import pandas as pd
import pytest
import shutil
import yaml

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
handler = logging.StreamHandler()
handler.setLevel(logging.DEBUG)
logger.addHandler(handler)

device_name = "aws_guardduty"
schema_name = "logs_omss_aws_guardduty"
meta_schema_name = "logs_base"
meta_schema_version = "v001.000.000"
uses_native_fields = True

expected_tables = ["logs_alerts", schema_name]
root_test_dir = Path(__file__).resolve().parent.parent.parent
global_date_time = datetime.now().strftime("%d%H%M%S")
job_output_path = "tests/job_outputs"
test_log_path = "tests/test_logs"
root_log_path = f"tests/test_logs/{'openmss' if (uses_native_fields) else 'hypercol'}"
Path(os.path.join(os.getcwd(), f"{job_output_path}")).mkdir(exist_ok=True)
Path(os.path.join(os.getcwd(), f"{test_log_path}")).mkdir(exist_ok=True)
Path(os.path.join(os.getcwd(), f"{root_log_path}")).mkdir(exist_ok=True)
Path(os.path.join(os.getcwd(), f"{root_log_path}/temp")).mkdir(exist_ok=True)

paths = {
    "log_path": os.path.join(os.getcwd(), f"{root_log_path}/{device_name}"),
    "temp_path": os.path.join(os.getcwd(), f"{root_log_path}/temp"),
    "dfe_package_path": os.path.join(
        os.getcwd(), "tests/resources/pipeline_resources/test_harness_dfe_package.yaml"
    ),
    "hunt_log_path": os.path.join(
        os.getcwd(), f"{root_log_path}/temp/hunt_logs_{device_name}"
    ),
    "hunt_checkpoint_path": os.path.join(
        os.getcwd(), f"{root_log_path}/temp/hunt_checkpoint_{device_name}"
    ),
    "results_file_path": os.path.join(
        os.getcwd(),
        f"{root_log_path}/{device_name}/{device_name}-rules-{global_date_time}.json",
    ),
    "converted_rule_path": os.path.join(
        os.getcwd(), f"tests/job_outputs/rule_builder_output_{global_date_time}"
    ),
}


@pytest.fixture
def test_target_config_data(target_config_data):
    """
    Test to ensure the target config data fixture works as expected.
    """
    assert target_config_data is not None, "Target config data should not be None"
    logger.info(f"Target Config Data: {target_config_data}")


@pytest.fixture(scope="module")
def ch_client(dfe_config_fixtures):
    config = DFEConfigLoader.read_clickhouse_config(
        target_name=dfe_config_fixtures["global_settings"]["default_target"],
        targets_file_path=dfe_config_fixtures["global_settings"]["target_path"],
    )
    print(f"*** using config {config}")

    ch_client = ClickHouseManager.get_instance(logger, config).get_clickhouse_client()

    yield ch_client


@pytest.fixture
def test_clickhouse_connection(ch_client):
    """
    Test to ensure ClickHouse client fixture works and can execute a basic query.
    """
    try:
        result = ch_client.execute("SELECT 1")
        assert result[0][0] == 1, "ClickHouse should return 1 for SELECT 1 query"
        logger.info("ClickHouse connection is working fine.")
    except Exception as e:
        pytest.skip(f"ClickHouse connection failed: {e}")


@pytest.fixture(scope="session")
def test_db_name():
    """Creates a test database name based off the current date time"""
    return "rule_compilation_04212734"


""" Creates a test dfe_package configuration """


@pytest.fixture(scope="session")
def dfe_config_fixtures(tmp_path_factory, test_db_name):
    """Create test configuration with proper paths."""
    temp_dir = tmp_path_factory.mktemp("test_dfe_schema_output")
    targets_dir = tmp_path_factory.mktemp("test_targets")
    targets_path = targets_dir / "dfe_targets.yaml"
    targets_data = {
        "default_target": "test_schemas",
        "targets": {
            "test_schemas": {
                "ch_host": "localhost",
                "ch_port": 8123,
                "ch_database": "default",
                "hunt_config_path": "tests/tmp/test_hunts",
                "hunt_rules_path": "tests/tmp/test_rules/executables",
            }
        },
    }

    with open(targets_path, "w") as f:
        yaml.dump(targets_data, f)

    config_data = {
        "global_settings": {
            "schema_common_version": "v001.001.007",
            "derived_schema_paths": "tests/resources/test_schemas/stable_schemas",
            "schema_output_path": "tests/job_outputs/test_schema_output",
            "default_target": "test_schemas",
            "use_json_feature": False,
            "target_path": str(targets_path),
        },
        "organisations": [
            {"cluster_name": "", "org_id": test_db_name, "schemas": expected_tables}
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
            schema_name: {
                "name": schema_name,
                "meta_schema": f"{meta_schema_name}.csv",
                "meta_schema_version": meta_schema_version,
                "derived_schema_file_path": f"{schema_name}/{schema_name}_sub.csv"
                if (meta_schema_name != "logs_base")
                else None,
                "additional_fields_config": f"{schema_name}/{schema_name}_add.csv",
                "derived_schema_ttl": 7,
            },
        },
    }
    yield config_data


def build_and_apply_schemas(dfe_logger, dfe_config_fixtures):
    """Builds the schema based on the sample schema (in ./elastic_watcher_converter/sample_schemas)"""
    dfe_sample_schema_output = Path(
        dfe_config_fixtures["global_settings"]["schema_output_path"]
    )

    if os.path.exists(dfe_config_fixtures["global_settings"]["schema_output_path"]):
        shutil.rmtree(dfe_config_fixtures["global_settings"]["schema_output_path"])

    dfe_sample_schema_output.mkdir(parents=True, exist_ok=True)

    try:
        SchemaController.build_schemas(
            args_dfe_package_file_path=paths["dfe_package_path"],
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
            args_log_path=paths["temp_path"],
            args_target=dfe_config_fixtures["global_settings"]["default_target"],
            args_target_file_path=dfe_config_fixtures["global_settings"]["target_path"],
        )

        SchemaController.apply_schemas(
            args_dfe_package_file_path=paths["dfe_package_path"],
            args_schema_directory=Path(
                dfe_config_fixtures["global_settings"]["derived_schema_paths"]
            ),
            args_do_add_roles=dfe_config_fixtures["apply_schemas"]["do_add_roles"],
            args_use_json_feature=dfe_config_fixtures["global_settings"][
                "use_json_feature"
            ],
            args_log_path=paths["temp_path"],
            args_target=dfe_config_fixtures["global_settings"]["default_target"],
            args_target_file_path=dfe_config_fixtures["global_settings"]["target_path"],
        )

    except Exception as e:
        dfe_logger.error(
            f"An error occured building and applying schemas: {str(e)}", exc_info=True
        )
        raise AssertionError(
            f"An error occured building and applying schemas: {str(e)}"
        )


""" Loads the rule files of the given directory """


def load_hunt_rule_names(directory, dfe_logger):
    dfe_logger.info(f"Loading rules from the rule directory '{directory}'")

    rule_names = []
    for root, _, files in os.walk(directory):
        dfe_logger.info(f"Files: {files}")
        for file_name in files:
            rule_file_path = os.path.join(root, file_name)
            with open(rule_file_path, "r", encoding="utf-8") as f:
                rule_names.append(file_name.replace(".jinja2", ""))

    return rule_names


def parse_and_convert_watchers(output_path, dfe_logger) -> pd.DataFrame:
    """Parse and convert watchers from a given directory and save SQL scripts"""
    try:
        es_watchers = f"resources/test_watchers/{device_name}/smd"
        hunt_params = {
            "es_watchers": str(root_test_dir / es_watchers),
            "customer_org_id": f"rule_compilation_{global_date_time}",
            "source": device_name,
            "alert_table": "logs_alerts",
        }
        Path(output_path).mkdir(exist_ok=True)
        dfe_logger.info(
            f"#### Writing watchers from '{hunt_params['es_watchers']}' to '{output_path}' ####"
        )
        parser = WatcherParser(
            schema_map_path="tests/resources/test_schema_mapping/openmss",
            logger=dfe_logger,
            native_fields=uses_native_fields,
        )
        watcher_df = parser.parse_watchers(hunt_params["es_watchers"])
        converter = WatcherConverter(output_path, logger=dfe_logger)
        converter.convert_and_write_sql(watcher_df)
        return watcher_df

    except Exception as e:
        dfe_logger.error(
            f"Wacher Parser failed for {hunt_params['es_watchers']}: \n {str(e)}",
            exc_info=True,
        )
        raise AssertionError(
            f"Watcher Parsing validation error for {hunt_params['es_watchers']}: {str(e)}"
        )


""" Compiles and tests the rules based off dummy hunts and records successes/failures """


def compile_and_test_rules(
    rule_file_names, watcher_df, dfe_config_fixtures, test_db_name, dfe_logger
):
    """Compiles and tests the rules based off dummy hunts and records successes/failures"""
    # Ensure database exists before running hunts
    try:
        ch_client = ClickHouseManager.get_instance(
            dfe_logger, dfe_config_fixtures
        ).get_clickhouse_client()
        with ch_client:
            ch_client.execute(f"CREATE DATABASE IF NOT EXISTS {test_db_name}")
            dfe_logger.info(f"✅ Ensured database {test_db_name} exists")
    except Exception as e:
        dfe_logger.error(f"Failed to create database {test_db_name}: {e}")
        raise

    if os.path.exists(paths["hunt_log_path"]):
        shutil.rmtree(paths["hunt_log_path"])
    if os.path.exists(paths["hunt_checkpoint_path"]):
        shutil.rmtree(paths["hunt_checkpoint_path"])
    Path(paths["hunt_log_path"]).mkdir(exist_ok=True)
    Path(paths["hunt_checkpoint_path"]).mkdir(exist_ok=True)

    results = {
        "total": len(rule_file_names),
        "successes": 0,
        "failures": 0,
        "failed_rules": [],
    }

    config = DFEConfigLoader.read_target_config(
        target_name=dfe_config_fixtures["global_settings"]["default_target"],
        targets_file_path=dfe_config_fixtures["global_settings"]["target_path"],
    )

    for rule_name in rule_file_names:
        dfe_logger.info(f"Running rule {rule_name}...")
        where_clause = watcher_df.loc[
            watcher_df["watcher_id"] == rule_name, "where_clause"
        ]
        if len(where_clause) > 1:
            dfe_logger.warning(
                f"There are more than 1 entries for {rule_name} in the watcher parser output. Proceeding using the first value."
            )
        whitelisting = where_clause.iloc[0]["customers"]
        env = Environment(loader=FileSystemLoader(paths["converted_rule_path"]))
        hunt = Hunt(
            name=rule_name,
            cron="* * * * *",
            log_buffer=60,
            global_source_table_name=schema_name,
            global_target_table_name="logs_alerts",
            checkpoint_timestamp_field="timestamp_load",
            customer=test_db_name,
            rules=[{"rule_name": rule_name}],
            customer_filters=whitelisting,
            hunt_log_path=paths["hunt_log_path"],
            target_config_data=config,
            checkpoint_destination="clickhouse",
            hunt_checkpoint_path=paths["hunt_checkpoint_path"],
            dfe_logger=dfe_logger,
        )
        hunt.convert_yaml_to_sql(env=env, org_id=test_db_name, customer_filters={})
        hunt.execute_hunt(
            customer=test_db_name, scheduled_start_time=datetime.now(timezone.utc)
        )

        for file_name in os.listdir(paths["hunt_log_path"]):
            if rule_name in file_name:
                file_path = os.path.join(paths["hunt_log_path"], file_name)
                with open(file_path, "r") as hunt_log_file:
                    message = hunt_log_file.readlines()
                    last_line = message[-1].strip()
                if rule_name in last_line:
                    dfe_logger.info(f"Rule {rule_name}: Success")
                    results["successes"] += 1
                else:
                    dfe_logger.error(f"Rule {rule_name}: Fail")
                    results["failures"] += 1
                    results["failed_rules"].append(
                        {"rule": rule_name, "error_message": "".join(message)}
                    )

    return results


""" Tests the execution of all rules in the 'rules_directory' based on the built schema """


def test_rules_execution(test_db_name, dfe_config_fixtures, ch_client):
    try:
        dfe_logger = DFELog.get_root_logger(
            logging_directory=paths["log_path"], log_file_prefix="client_config_log"
        )

        build_and_apply_schemas(
            dfe_logger=dfe_logger, dfe_config_fixtures=dfe_config_fixtures
        )

        dfe_logger = DFELog.get_root_logger(
            logging_directory=paths["log_path"],
            log_file_prefix=f"test_{'openmss' if (uses_native_fields == True) else 'hypercol'}_{device_name}_detections",
        )

        Path(paths["converted_rule_path"]).mkdir(exist_ok=True)
        paths["converted_rule_path"] = os.path.join(
            paths["converted_rule_path"], device_name
        )
        if os.path.exists(paths["converted_rule_path"]):
            shutil.rmtree(paths["converted_rule_path"])

        watcher_df = parse_and_convert_watchers(
            output_path=paths["converted_rule_path"], dfe_logger=dfe_logger
        )

        rule_file_names = load_hunt_rule_names(
            directory=paths["converted_rule_path"], dfe_logger=dfe_logger
        )
        results = compile_and_test_rules(
            rule_file_names=rule_file_names,
            watcher_df=watcher_df,
            dfe_config_fixtures=dfe_config_fixtures,
            test_db_name=test_db_name,
            dfe_logger=dfe_logger,
        )

        with open(paths["results_file_path"], "w") as f:
            json.dump(results, f, indent=4)

        json.dumps(results, indent=4)
        dfe_logger.info(f"Total Rules: {results['total']}")
        dfe_logger.info(f"Successful Rules: {results['successes']}")
        dfe_logger.info(f"Failed Rules: {results['failures']}")

        assert results["failures"] == 0, (
            f"There is {results['failures']} rules failing to compile in the rule directory."
        )

    except Exception as e:
        dfe_logger.error(f"Wacher Compilation Test Failed: \n {str(e)}.", exc_info=True)
        raise e
