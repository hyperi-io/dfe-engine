import pytest
from dfe_engine.clickhouse.clickhouse_errors_mapping import ClickHouseErrorHandler


@pytest.fixture
def init_clickhouse_error_handler():
    return ClickHouseErrorHandler


def test_parse_error(init_clickhouse_error_handler, test_clickhouse_errors_mapping):
    error_code = test_clickhouse_errors_mapping["error_code"]
    error = Exception(test_clickhouse_errors_mapping["error_message"])
    result = init_clickhouse_error_handler.parse_error(error)

    assert(result["code"] == error_code)
    assert(result["type"] == init_clickhouse_error_handler.ERROR_MAPPINGS[error_code]["type"])
    assert(result["message"] == test_clickhouse_errors_mapping["error_message"])
    assert(result["user_message"] == init_clickhouse_error_handler.ERROR_MAPPINGS[error_code]["message"])
    assert(result["actionable"] == test_clickhouse_errors_mapping["is_actionable"])