import pytest

TEST_CLICKHOUSE_CLIENT_WRAPPER_EXECUTE = [
    {
        "name": "single_select",
        "query": "SELECT 1;",
        "expected_result": [(1,)]
    },
    {
        "name": "double_select",
        "query": "SELECT 1; SELECT 2;",
        "expected_result": [(1,), (2,)]
    },
    {
        "name": "triple_select",
        "query": "SELECT 1; SELECT 2; SELECT 1;",
        "expected_result": [(1,), (2,), (1,)]
    },
    {
        "name": "describe",
        "query": "DESCRIBE TABLE system.numbers;",
        "expected_result": [("number", "UInt64", "", "", "", "", "")]
    },
    {
        "name": "desc",
        "query": "DESC TABLE system.numbers;",
        "expected_result": [("number", "UInt64", "", "", "", "", "")]
    },
    {
        "name": "exists",
        "query": "EXISTS TABLE system.numbers;",
        "expected_result": [(1,)]
    },
    {
        "name": "explain",
        "query": "EXPLAIN SELECT * FROM system.numbers LIMIT 10;",
        "expected_result": [("Expression ((Project names + (Projection + Change column names to column identifiers)))",), ("  Limit (preliminary LIMIT (without OFFSET))",), ("    ReadFromSystemNumbers",)]
    },
    {
        "name": "show",
        "query": "SHOW CREATE TABLE system.numbers;",
        "expected_result": [("CREATE TABLE system.numbers\n(\n    `number` UInt64\n)\nENGINE = SystemNumbers\nCOMMENT 'Generates all natural numbers, starting from 0 (to 2^64 - 1, and then again) in sorted order.'",)]
    }
]

@pytest.fixture(params = TEST_CLICKHOUSE_CLIENT_WRAPPER_EXECUTE, ids = lambda x: x["name"])
def test_clickhouse_client_wrapper_execute(request):
    return request.param