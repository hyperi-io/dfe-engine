import os
import pytest
import yaml
import logging


@pytest.fixture(scope="session")
def dfe_config_fixtures():
    config_data = {
        "global_settings": {
            "schema_common_version": "v001.000.000",
            "schema_output_path": "../.dfe_schema_output/",
            "default_target": "integration",
            "target_path": "~/.dfe/dfe_targets.yaml",
            "vector_files": {
                "standard": [
                    "ingestion_pipeline_enrichment_standard",
                    "ingestion_pipeline_enrichment_standard_custom",
                ],
                "geoip": ["ingestion_pipeline_enrichment_geoip"],
            },
            "vector_files": {
                "vector": ["ingestion_pipeline_templates"]
            },
            "derived_schema_paths": "tests/resources/test_schemas/filebeats_schemas",
            "meta_schema_paths": "../post_build_artefacts/dfe_meta_schemas_package",
        },
        "build_schemas": {
            "no_cluster_declarations_needed": True,
            "use_replicated_merge_tree": False,
            "use_shared_merge_tree": False,
        },
        "apply_schemas": {
            "do_add_roles": False,
            "do_add_columns": True,
        },
        "organisations": [
            {
                "org_id": "filebeat_speed",
                "cluster_name": "",
                "schemas": ["logs_beats_filebeat_activemq"],
            }
        ],
        "schemas": {
            "logs_beats_filebeat_activemq": {
                "name": "logs_beats_filebeat_activemq",
                "meta_schema": "logs_beats_filebeat.csv",
                "meta_schema_version": "v001.000.000",
                "derived_schema_file_path": "logs_beats_filebeat/logs_beats_filebeat_activemq.csv",
                "derived_schema_ttl": 90,
            },
        },
    }

    return config_data


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)8s | %(module)20s:%(lineno)4d | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

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
    logger.info(
        "\n===================== test_schemas_beats_performance_tuning Test Summary ====================="
    )
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


@pytest.fixture(scope="session")
def setup_paths():
    dfe_root_log_path = os.path.join(os.getcwd(), "tmp", "logs_path")
    os.makedirs(dfe_root_log_path, exist_ok=True)
    return dfe_root_log_path


@pytest.fixture(scope="session")
def dfe_package(dfe_config_fixtures):
    dfe_package_path = os.path.join(os.getcwd(), "dfe_package_testing.yaml")

    with open(dfe_package_path, "w") as f:
        yaml.dump(dfe_config_fixtures, f)

    return dfe_package_path
