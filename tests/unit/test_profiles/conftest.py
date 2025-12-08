import pytest
import logging
from unittest.mock import MagicMock, mock_open

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
        "\n===================== test_profiles Test Summary ====================="
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


MOCK_CONFIG = """
default_target: target_1
targets:
  target_1:
    ch_host: 'localhost'
    ch_port: 8123
    ch_username: 'user1'
    ch_password: 'password1'
    ip_config_bucket_name: 'dev_config_bucket_afterburner'
    ip_config_bucket_region: 'ap-southeast-2'
    ip_config_standard_enrichment_path: 'vector_templates/standard_enrichment_files'
    ip_config_receiver_path: 'vector_templates/vector_receiver'
    ip_config_geo_ip_path: 'vector_templates/geoip'
    ip_templates_path: 'vector_templates'
    hunt_config_path: 'dfecli/stable/hunt'
    hunt_rules_path: 'dfecli/stable/rules'
  target_2:
    ch_host: 'localhost'
    ch_port: 8123
    ch_username: 'user1'
    ch_password: 'password1'
    ip_config_bucket_name: 'dev_config_bucket_afterburner'
    ip_config_bucket_region: 'ap-southeast-2'
    ip_config_standard_enrichment_path: 'vector_templates/standard_enrichment_files'
    ip_config_receiver_path: 'vector_templates/vector_receiver'
    ip_config_geo_ip_path: 'vector_templates/geoip'
    ip_templates_path: 'vector_templates'
    hunt_config_path: 'dfecli/stable/hunts'
    hunt_rules_path: 'dfecli/stable/rules'
"""


@pytest.fixture
def mock_env_variables(monkeypatch):
    monkeypatch.setenv("DFE_CH_HOST", "localhost")
    monkeypatch.setenv("DFE_CH_PORT", "8123")
    monkeypatch.setenv("DFE_CH_USERNAME", "user1")
    monkeypatch.setenv("DFE_CH_PASSWORD", "password1")


@pytest.fixture
def mock_config_file(monkeypatch):
    m = mock_open(read_data=MOCK_CONFIG)
    monkeypatch.setattr("builtins.open", m)
    return m


@pytest.fixture
def mock_path_exists(monkeypatch):
    monkeypatch.setattr("pathlib.Path.exists", MagicMock(return_value=True))


@pytest.fixture
def test_logger():
    return logging.getLogger("test")
