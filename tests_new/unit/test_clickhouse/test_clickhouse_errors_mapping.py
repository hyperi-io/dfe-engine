import pytest
from dfe_engine.clickhouse.clickhouse_errors_mapping import ClickHouseErrorHandler


@pytest.fixture
def init_clickhouse_error_handler():
    return ClickHouseErrorHandler


def test_parse_error(init_clickhouse_error_handler, test_clickhouse_errors_mapping_parse_error):
    error_code = test_clickhouse_errors_mapping_parse_error["error_code"]
    error_type = test_clickhouse_errors_mapping_parse_error["error_type"]
    error_category = test_clickhouse_errors_mapping_parse_error["error_category"]
    error_message = test_clickhouse_errors_mapping_parse_error["error_message"]
    error_user_message = test_clickhouse_errors_mapping_parse_error["error_user_message"]
    error_is_actionable = test_clickhouse_errors_mapping_parse_error["error_is_actionable"]

    error = Exception(test_clickhouse_errors_mapping_parse_error["error"])

    result = init_clickhouse_error_handler.parse_error(error)
    assert(result["code"] == error_code)
    assert(result["type"] == error_type)
    assert(result["category"] == error_category)
    assert(result["message"] == error_message)
    assert(result["user_message"] == error_user_message)
    assert(result["is_actionable"] == error_is_actionable)


def test_get_error_message(init_clickhouse_error_handler, test_clickhouse_errors_mapping_get_error_message):
    error_code = test_clickhouse_errors_mapping_get_error_message["error_code"]
    raises = test_clickhouse_errors_mapping_get_error_message.get("raises", False)

    if (raises):
        with pytest.raises(raises["exception"]) as exc_info:
            init_clickhouse_error_handler.get_error_type(error_code)
        assert(raises["message"] in str(exc_info.value))
        return
    
    error_message = test_clickhouse_errors_mapping_get_error_message["error_message"]

    result = init_clickhouse_error_handler.get_error_message(error_code)
    assert(result == error_message)


def test_get_error_type(init_clickhouse_error_handler, test_clickhouse_errors_mapping_get_error_type):
    error_code = test_clickhouse_errors_mapping_get_error_type["error_code"]
    raises = test_clickhouse_errors_mapping_get_error_type.get("raises", False)

    if (raises):
        with pytest.raises(raises["exception"]) as exc_info:
            init_clickhouse_error_handler.get_error_type(error_code)
        assert(raises["message"] in str(exc_info.value))
        return
    
    error_type = test_clickhouse_errors_mapping_get_error_type["error_type"]

    result = init_clickhouse_error_handler.get_error_type(error_code)
    assert(result == error_type)


def test_get_error_category(init_clickhouse_error_handler, test_clickhouse_errors_mapping_get_error_category):
    error_code = test_clickhouse_errors_mapping_get_error_category["error_code"]
    raises = test_clickhouse_errors_mapping_get_error_category.get("raises", False)

    if (raises):
        with pytest.raises(raises["exception"]) as exc_info:
            init_clickhouse_error_handler.get_error_category(error_code)
        assert(raises["message"] in str(exc_info.value))
        return
    
    error_category = test_clickhouse_errors_mapping_get_error_category["error_category"]

    result = init_clickhouse_error_handler.get_error_category(error_code)
    assert(result == error_category)


def test_is_resource_limit_error(init_clickhouse_error_handler, test_clickhouse_errors_mapping_is_resource_limit_error):
    error_code = test_clickhouse_errors_mapping_is_resource_limit_error["error_code"]
    raises = test_clickhouse_errors_mapping_is_resource_limit_error.get("raises", False)

    if (raises):
        with pytest.raises(raises["exception"]) as exc_info:
            init_clickhouse_error_handler.is_resource_limit_error(error_code)
        assert(raises["message"] in str(exc_info.value))
        return
    
    is_resource_limit_error = test_clickhouse_errors_mapping_is_resource_limit_error["is_resource_limit_error"]

    result = init_clickhouse_error_handler.is_resource_limit_error(error_code)
    assert(result == is_resource_limit_error)


def test_is_retryable_error(init_clickhouse_error_handler, test_clickhouse_errors_mapping_is_retryable_error):
    error_code = test_clickhouse_errors_mapping_is_retryable_error["error_code"]
    raises = test_clickhouse_errors_mapping_is_retryable_error.get("raises", False)

    if (raises):
        with pytest.raises(raises["exception"]) as exc_info:
            init_clickhouse_error_handler.is_retryable_error(error_code)
        assert(raises["message"] in str(exc_info.value))
        return
    
    is_retryable_error = test_clickhouse_errors_mapping_is_retryable_error["is_retryable_error"]

    result = init_clickhouse_error_handler.is_retryable_error(error_code)
    assert(result == is_retryable_error)


def test_format_error_for_api(init_clickhouse_error_handler, test_clickhouse_errors_mapping_format_error_for_api):
    expected_dict = test_clickhouse_errors_mapping_format_error_for_api["expected_dict"]
    
    error = Exception(test_clickhouse_errors_mapping_format_error_for_api["error"])

    result = init_clickhouse_error_handler.format_error_for_api(error)
    assert(result == expected_dict)