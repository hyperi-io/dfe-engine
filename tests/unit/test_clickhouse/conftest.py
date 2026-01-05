import pytest

TEST_CLICKHOUSE_ERRORS_MAPPING = [
    {
        "name": "test_code_60_success",
        "error_message": "Code: 60. Memory limit exceeded (for query) exceeded: would use 4.12 GiB (attempt to allocate chunk of 134217728 bytes) maximum: 4.00 GiB",
        "error_code": 60,
        "error_type": "memory_limit",
        "is_actionable": True
    },
    {
        "name": "test_code_62_success",
        "error_message": "Code: 62. DB::Exception: Query size exceeded the maximum (max_query_size = 262144), consider increasing max_query_size setting",
        "error_code": 62,
        "error_type": "query_size_limit",
        "is_actionable": True
    },
    {
        "name": "test_code_159_success",
        "error_message": "Code: 159. DB::Exception: Timeout exceeded: elapsed 30.001 seconds, maximum: 30",
        "error_code": 159,
        "error_type": "timeout",
        "is_actionable": True
    }
]

@pytest.fixture(params = TEST_CLICKHOUSE_ERRORS_MAPPING)
def test_clickhouse_errors_mapping(request):
    return request.param