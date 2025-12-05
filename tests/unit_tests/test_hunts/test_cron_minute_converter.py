import pytest
from dfecli.dfe_async_hunts.hunts.hunts import Hunt

TESTS = [
    {"name": "Testing Specification", "cron": "* * * * *", "minutes": 1},
    {"name": "Minutes Specification", "cron": "*/10 * * *", "minutes": 1 * 10},
    {"name": "Hours Specification", "cron": "1 */1 * * *", "minutes": 60 * 1},
    {"name": "Days Specification", "cron": "1 1 */1 * *", "minutes": 24 * 60 * 1},
    {"name": "Month Specification", "cron": "1 1 1 */1 *", "minutes": -1},
]


@pytest.mark.parametrize("cron_test", TESTS, ids=[test["name"] for test in TESTS])
def test_cron_minute_converter(cron_test):
    hunt = Hunt(
        name="test_cron_minute_converter_hunt",
        cron=cron_test["cron"],
        log_buffer=60,
        global_source_table_name="test123",
        global_target_table_name="logs_alerts",
        checkpoint_timestamp_field="timestamp_load",
        customer="test123",
        rules=[{"rule_name": "test123"}],
        customer_filters={"test123": "test123"},
        hunt_log_path="test123",
        target_config_data="test123",
        checkpoint_destination="clickhouse",
        hunt_checkpoint_path="test123",
        dfe_logger="test123",
    )
    assert hunt.convert_cron_to_minutes() == cron_test["minutes"]
