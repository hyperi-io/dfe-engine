import pytest

TEST_CLICKHOUSE_ERRORS_MAPPING = [
    {
        "name": "test_code_60_success",
        "error": "Code: 60. Memory limit exceeded (for query) exceeded: would use 4.12 GiB (attempt to allocate chunk of 134217728 bytes) maximum: 4.00 GiB",
        "error_code": 60,
        "error_type": "memory_limit",
        "error_message": "Code: 60. Memory limit exceeded (for query) exceeded: would use 4.12 GiB (attempt to allocate chunk of 134217728 bytes) maximum: 4.00 GiB",
        "error_user_message": "Memory limit exceeded while processing the request. This typically occurs when working with large datasets. Try: 1) Add LIMIT clauses to reduce result size, 2) Use sampling (SAMPLE clause) for analysis, 3) Increase max_memory_usage setting, or 4) Optimize query with better filtering.",
        "is_actionable": True
    },
    {
        "name": "test_code_62_success",
        "error": "Code: 62. DB::Exception: Query size exceeded the maximum (max_query_size = 262144), consider increasing max_query_size setting",
        "error_code": 62,
        "error_type": "query_size_limit",
        "error_message": "Code: 62. DB::Exception: Query size exceeded the maximum (max_query_size = 262144), consider increasing max_query_size setting",
        "error_user_message": "Query size limit exceeded. The SQL statement is too large to process. Try: 1) Break complex queries into smaller operations, 2) Use temporary tables for intermediate results, 3) Increase max_query_size setting, or 4) Simplify the query structure.",
        "is_actionable": True
    },
    {
        "name": "test_code_159_success",
        "error": "Code: 159. DB::Exception: Timeout exceeded: elapsed 30.001 seconds, maximum: 30",
        "error_code": 159,
        "error_type": "timeout",
        "error_message": "Code: 159. DB::Exception: Timeout exceeded: elapsed 30.001 seconds, maximum: 30",
        "error_user_message": "Query execution timed out. The operation took too long to complete. Try: 1) Add indexes on frequently filtered columns, 2) Use OPTIMIZE TABLE for better performance, 3) Increase timeout settings (max_execution_time), or 4) Break the query into smaller chunks.",
        "is_actionable": True
    }
]

@pytest.fixture(params = TEST_CLICKHOUSE_ERRORS_MAPPING)
def test_clickhouse_errors_mapping(request):
    return request.param