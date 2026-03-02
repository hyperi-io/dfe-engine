import pytest
from dfe_engine.hunts.hunt import Hunt

TESTS = [
    {"name": "Testing Specification", "cron": "* * * * *", "minutes": 1},
    {"name": "Minutes Specification", "cron": "*/10 * * *", "minutes": 1 * 10},
    {"name": "Hours Specification", "cron": "1 */1 * * *", "minutes": 60 * 1},
    {"name": "Days Specification", "cron": "1 1 */1 * *", "minutes": 24 * 60 * 1},
    {"name": "Month Specification", "cron": "1 1 1 */1 *", "minutes": -1},
]


@pytest.mark.parametrize("cron_test", TESTS, ids=[test["name"] for test in TESTS])
def test_cron_minute_converter(cron_test, tmp_path):
    hunt = Hunt(
        name="test_cron_minute_converter_hunt",
        cron=cron_test["cron"],
        log_buffer=60,
        global_source_table_name="test_source",
        global_target_table_name="logs_alerts",
        checkpoint_timestamp_field="timestamp_load",
        customer="test_customer",
        rules=[{"rule_name": "test_rule"}],
        customer_filters={"test_customer": "org_id = 'test'"},
        hunt_log_path=str(tmp_path / "hunt_logs"),
        target_config_data="test_config",
        checkpoint_destination="clickhouse",
        hunt_checkpoint_path=str(tmp_path / "checkpoints"),
    )
    assert hunt.convert_cron_to_minutes() == cron_test["minutes"]
