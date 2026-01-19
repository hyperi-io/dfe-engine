import pytest
from dfe_engine.clickhouse.clickhouse_errors_mapping import ClickHouseErrorHandler


@pytest.fixture
def init_clickhouse_error_handler():
    return ClickHouseErrorHandler


def test_parse_error(init_clickhouse_error_handler, test_clickhouse_errors_mapping):
    error_code = test_clickhouse_errors_mapping["error_code"]
    error_type = test_clickhouse_errors_mapping["error_type"]
    error_message = test_clickhouse_errors_mapping["error_message"]
    error_user_message = test_clickhouse_errors_mapping["error_user_message"]
    error_actionable = test_clickhouse_errors_mapping["is_actionable"]

    error = Exception(test_clickhouse_errors_mapping["error"])

    result = init_clickhouse_error_handler.parse_error(error)

    assert result["code"] == error_code
    assert result["type"] == error_type
    assert result["message"] == error_message
    assert result["user_message"] == error_user_message
    assert result["actionable"] == error_actionable
