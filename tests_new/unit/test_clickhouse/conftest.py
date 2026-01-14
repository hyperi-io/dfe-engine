import pytest
from pydantic import ValidationError

TEST_CLICKHOUSE_ERRORS_MAPPING_PARSE_ERROR = [
    {
        "name": "test_code_36_success",
        "error": "Code: 36. DB::Exception: Table default.non_existent_table doesn't exist. (UNKNOWN_TABLE)",
        "error_code": 36,
        "error_category": "syntax",
        "error_type": "unknown_table",
        "error_message": "Code: 36. DB::Exception: Table default.non_existent_table doesn't exist. (UNKNOWN_TABLE)",
        "error_user_message": "Unknown table referenced in the query. Verify: 1) Table name is spelled correctly, 2) Table exists in the current database, 3) Correct database is selected (USE database_name), or 4) Table permissions allow access.",
        "error_is_actionable": True
    },
    {
        "name": "test_code_43_success",
        "error": "Code: 43. DB::Exception: Unknown function fakeFunction. (UNKNOWN_FUNCTION)",
        "error_code": 43,
        "error_category": "syntax",
        "error_type": "function_not_found",
        "error_message": "Code: 43. DB::Exception: Unknown function fakeFunction. (UNKNOWN_FUNCTION)",
        "error_user_message": "Function not found in the query. Check: 1) Function name spelling, 2) Function exists in ClickHouse (check documentation), 3) Required parameters are provided, or 4) Custom functions are properly installed.",
        "error_is_actionable": True
    },
    {
        "name": "test_code_53_success",
        "error": "Code: 53. DB::Exception: Type mismatch in IN or VALUES section. Expected: UInt64. Got: String. (TYPE_MISMATCH)",
        "error_code": 53,
        "error_category": "data",
        "error_type": "type_mismatch",
        "error_message": "Code: 53. DB::Exception: Type mismatch in IN or VALUES section. Expected: UInt64. Got: String. (TYPE_MISMATCH)",
        "error_user_message": "Data type mismatch in the query. Verify: 1) Column data types match the values being inserted/compared, 2) CAST functions are used for type conversions, 3) Schema definitions are correct, or 4) Date/time formats match expected patterns.",
        "error_is_actionable": True
    },
    {
        "name": "test_code_57_success",
        "error": "Code: 57. DB::Exception: Table default.users already exists. (TABLE_ALREADY_EXISTS)",
        "error_code": 57,
        "error_category": "exists",
        "error_type": "table_already_exists",
        "error_message": "Code: 57. DB::Exception: Table default.users already exists. (TABLE_ALREADY_EXISTS)",
        "error_user_message": "Table already exists in the database. If you want to recreate it, first drop the existing table with: DROP TABLE table_name. If you want to modify the schema, use ALTER TABLE instead.",
        "error_is_actionable": True
    },
    {
        "name": "test_code_60_success",
        "error": "Code: 60. DB::Exception: Memory limit (for query) exceeded: would use 10.00 GiB (attempt to allocate chunk of 134217728 bytes), maximum: 9.31 GiB. (MEMORY_LIMIT_EXCEEDED)",
        "error_code": 60,
        "error_category": "resource_limit",
        "error_type": "memory_limit",
        "error_message": "Code: 60. DB::Exception: Memory limit (for query) exceeded: would use 10.00 GiB (attempt to allocate chunk of 134217728 bytes), maximum: 9.31 GiB. (MEMORY_LIMIT_EXCEEDED)",
        "error_user_message": "Memory limit exceeded while processing the request. This typically occurs when working with large datasets. Try: 1) Add LIMIT clauses to reduce result size, 2) Use sampling (SAMPLE clause) for analysis, 3) Increase max_memory_usage setting, or 4) Optimize query with better filtering.",
        "error_is_actionable": True
    },
    {
        "name": "test_code_62_success",
        "error": "Code: 62. DB::Exception: Query is too large. Maximum allowed query size is 262144 bytes. (QUERY_IS_TOO_BIG)",
        "error_code": 62,
        "error_category": "query_limit",
        "error_type": "query_size_limit",
        "error_message": "Code: 62. DB::Exception: Query is too large. Maximum allowed query size is 262144 bytes. (QUERY_IS_TOO_BIG)",
        "error_user_message": "Query size limit exceeded. The SQL statement is too large to process. Try: 1) Break complex queries into smaller operations, 2) Use temporary tables for intermediate results, 3) Increase max_query_size setting, or 4) Simplify the query structure.",
        "error_is_actionable": True
    },
    {
        "name": "test_code_81_success",
        "error": "Code: 81. DB::Exception: Database analytics doesn't exist. (UNKNOWN_DATABASE)",
        "error_code": 81,
        "error_category": "not_found",
        "error_type": "database_not_found",
        "error_message": "Code: 81. DB::Exception: Database analytics doesn't exist. (UNKNOWN_DATABASE)",
        "error_user_message": "Database does not exist. Deploy schemas first using build-schemas and apply-schemas commands before running updates. Check: 1) Database name spelling, 2) Schema deployment completed successfully, or 3) Correct connection string.",
        "error_is_actionable": True
    },
    {
        "name": "test_code_107_success",
        "error": "Code: 107. DB::Exception: Table default.events already exists. (TABLE_ALREADY_EXISTS)",
        "error_code": 107,
        "error_category": "exists",
        "error_type": "table_already_exists",
        "error_message": "Code: 107. DB::Exception: Table default.events already exists. (TABLE_ALREADY_EXISTS)",
        "error_user_message": "Table already exists in the database. If you want to recreate it, first drop the existing table with: DROP TABLE table_name. If you want to modify the schema, use ALTER TABLE instead.",
        "error_is_actionable": True
    },
    {
        "name": "test_code_158_success",
        "error": "Code: 158. DB::Exception: Too many parameters: 100001. Maximum: 100000. (TOO_MANY_QUERY_PARAMETERS)",
        "error_code": 158,
        "error_category": "resource_limit",
        "error_type": "too_many_parameters",
        "error_message": "Code: 158. DB::Exception: Too many parameters: 100001. Maximum: 100000. (TOO_MANY_QUERY_PARAMETERS)",
        "error_user_message": "Too many parameters in the query. This usually happens with large IN clauses or prepared statements. Try: 1) Use temporary tables for large parameter lists, 2) Break queries into smaller operations, 3) Increase max_query_parameters setting, or 4) Use JOINs instead of large IN clauses.",
        "error_is_actionable": True
    },
    {
        "name": "test_code_159_success",
        "error": "Code: 159. DB::Exception: Timeout exceeded: elapsed 30.5 seconds, maximum: 30. (TIMEOUT_EXCEEDED)",
        "error_code": 159,
        "error_category": "timeout",
        "error_type": "timeout",
        "error_message": "Code: 159. DB::Exception: Timeout exceeded: elapsed 30.5 seconds, maximum: 30. (TIMEOUT_EXCEEDED)",
        "error_user_message": "Query execution timed out. The operation took too long to complete. Try: 1) Add indexes on frequently filtered columns, 2) Use OPTIMIZE TABLE for better performance, 3) Increase timeout settings (max_execution_time), or 4) Break the query into smaller chunks.",
        "error_is_actionable": True
    },
    {
        "name": "test_code_164_success",
        "error": "Code: 164. DB::Exception: Unknown setting invalid_setting_name. (UNKNOWN_SETTING)",
        "error_code": 164,
        "error_category": "configuration",
        "error_type": "unknown_setting",
        "error_message": "Code: 164. DB::Exception: Unknown setting invalid_setting_name. (UNKNOWN_SETTING)",
        "error_user_message": "Unknown or invalid setting specified. Check: 1) Setting name spelling, 2) Setting exists in ClickHouse (check documentation), 3) Setting is available in your ClickHouse version, or 4) Correct syntax for setting values.",
        "error_is_actionable": True
    },
    {
        "name": "test_code_173_success",
        "error": "Code: 173. DB::Exception: Too many simultaneous queries. Maximum: 100. (TOO_MANY_SIMULTANEOUS_QUERIES)",
        "error_code": 173,
        "error_category": "concurrency",
        "error_type": "too_many_queries",
        "error_message": "Code: 173. DB::Exception: Too many simultaneous queries. Maximum: 100. (TOO_MANY_SIMULTANEOUS_QUERIES)",
        "error_user_message": "Too many simultaneous queries running. The server is overloaded. Try: 1) Wait a few minutes and retry, 2) Reduce concurrent operations, 3) Check for long-running queries blocking others, or 4) Increase max_concurrent_queries setting.",
        "error_is_actionable": True
    },
    {
        "name": "test_code_192_success",
        "error": "Code: 192. DB::Exception: user_readonly: Not enough privileges. To execute this query it's necessary to have grant SELECT ON default.sensitive_data. (ACCESS_DENIED)",
        "error_code": 192,
        "error_category": "permission",
        "error_type": "access_denied",
        "error_message": "Code: 192. DB::Exception: user_readonly: Not enough privileges. To execute this query it's necessary to have grant SELECT ON default.sensitive_data. (ACCESS_DENIED)",
        "error_user_message": "Access denied due to insufficient permissions. Check: 1) User has required privileges (GRANT statements), 2) Correct user credentials, 3) Database/table permissions, or 4) RBAC roles are properly assigned.",
        "error_is_actionable": True
    },
    {
        "name": "test_code_193_success",
        "error": "Code: 193. DB::Exception: Authentication failed: password is incorrect or there is no user with such name. (AUTHENTICATION_FAILED)",
        "error_code": 193,
        "error_category": "authentication",
        "error_type": "authentication_failed",
        "error_message": "Code: 193. DB::Exception: Authentication failed: password is incorrect or there is no user with such name. (AUTHENTICATION_FAILED)",
        "error_user_message": "Authentication failed. Verify: 1) Username and password are correct, 2) User account exists and is not locked, 3) Connection uses proper authentication method, or 4) SSL/TLS certificates are valid (if required).",
        "error_is_actionable": True
    },
    {
        "name": "test_code_209_success",
        "error": "Code: 209. DB::NetException: Connection timeout: connect timed out after 10000ms. (SOCKET_TIMEOUT)",
        "error_code": 209,
        "error_category": "connection",
        "error_type": "connection_timeout",
        "error_message": "Code: 209. DB::NetException: Connection timeout: connect timed out after 10000ms. (SOCKET_TIMEOUT)",
        "error_user_message": "Connection attempt timed out. Check: 1) ClickHouse server is running and accessible, 2) Network connectivity to the host/port, 3) Firewall rules allow connections, 4) Increase connection_timeout setting if needed.",
        "error_is_actionable": True
    },
    {
        "name": "test_code_210_success",
        "error": "Code: 210. DB::NetException: Connection reset by peer, while reading from socket. (NETWORK_ERROR)",
        "error_code": 210,
        "error_category": "connection",
        "error_type": "connection_lost",
        "error_message": "Code: 210. DB::NetException: Connection reset by peer, while reading from socket. (NETWORK_ERROR)",
        "error_user_message": "Connection to database was lost during operation. This can happen due to: 1) Network instability, 2) Server restart, 3) Connection timeout, or 4) Firewall blocking. Try reconnecting and ensure network stability.",
        "error_is_actionable": True
    },
    {
        "name": "test_code_241_success",
        "error": "Code: 241. DB::Exception: Memory limit (for user) exceeded: would use 15.00 GiB, maximum: 10.00 GiB. (MEMORY_LIMIT_EXCEEDED)",
        "error_code": 241,
        "error_category": "resource_limit",
        "error_type": "memory_limit_query",
        "error_message": "Code: 241. DB::Exception: Memory limit (for user) exceeded: would use 15.00 GiB, maximum: 10.00 GiB. (MEMORY_LIMIT_EXCEEDED)",
        "error_user_message": "Memory limit exceeded for this specific query. Try: 1) Simplify the query with fewer JOINs or aggregations, 2) Use GROUP BY with LIMIT for large result sets, 3) Increase max_memory_usage_for_user setting, or 4) Consider using external aggregation for very large datasets.",
        "error_is_actionable": True
    },
    {
        "name": "test_code_253_success",
        "error": "Code: 253. DB::Exception: Table is in readonly mode since replica is not leader. (TABLE_IS_READ_ONLY)",
        "error_code": 253,
        "error_category": "replication",
        "error_type": "replica_readonly",
        "error_message": "Code: 253. DB::Exception: Table is in readonly mode since replica is not leader. (TABLE_IS_READ_ONLY)",
        "error_user_message": "Replica is in readonly mode and cannot accept write operations. This is normal for: 1) Read replicas in distributed setups, 2) During maintenance windows, or 3) When replication lag is too high. Direct writes to the primary/master node instead.",
        "error_is_actionable": True
    },
    {
        "name": "test_code_390_success",
        "error": "Code: 390. DB::Exception: Table default.missing_table doesn't exist. (UNKNOWN_TABLE)",
        "error_code": 390,
        "error_category": "not_found",
        "error_type": "table_not_found",
        "error_message": "Code: 390. DB::Exception: Table default.missing_table doesn't exist. (UNKNOWN_TABLE)",
        "error_user_message": "Table does not exist in the database. Ensure schemas are deployed first: run build-schemas then apply-schemas before attempting updates or queries. Check: 1) Table name spelling and case sensitivity, 2) Correct database selection, or 3) Schema deployment completed successfully.",
        "error_is_actionable": True
    },
    {
        "name": "test_database_string_case_success",
        "error": "Code: XXX. DB::Exception: Database analytics does not exist. (UNKNOWN_DATABASE)",
        "error_code": 81,
        "error_category": "not_found",
        "error_type": "database_not_found",
        "error_message": "Code: XXX. DB::Exception: Database analytics does not exist. (UNKNOWN_DATABASE)",
        "error_user_message": "Database does not exist. Deploy schemas first using build-schemas and apply-schemas commands before running updates. Check: 1) Database name spelling, 2) Schema deployment completed successfully, or 3) Correct connection string.",
        "error_is_actionable": True
    },
    {
        "name": "test_unmatching_case_success",
        "error": "Code: XXX. DB::Exception: Table default.missing_table doesn't exist. (UNKNOWN_TABLE)",
        "error_code": None,
        "error_category": "unknown",
        "error_type": "unknown_error",
        "error_message": "Code: XXX. DB::Exception: Table default.missing_table doesn't exist. (UNKNOWN_TABLE)",
        "error_user_message": "Database operation failed. Ensure schemas are deployed first using apply-schemas command before running updates. Check server logs for technical details if the issue persists.",
        "error_is_actionable": False
    }
]

TEST_CLICKHOUSE_ERRORS_MAPPING_GET_ERROR_MESSAGE = [
    {
        "name": "test_code_36_success",
        "error_code": 36,
        "error_message": "Unknown table referenced in the query. Verify: 1) Table name is spelled correctly, 2) Table exists in the current database, 3) Correct database is selected (USE database_name), or 4) Table permissions allow access."
    },
    {
        "name": "test_code_43_success",
        "error_code": 43,
        "error_message": "Function not found in the query. Check: 1) Function name spelling, 2) Function exists in ClickHouse (check documentation), 3) Required parameters are provided, or 4) Custom functions are properly installed."
    },
    {
        "name": "test_code_53_success",
        "error_code": 53,
        "error_message": "Data type mismatch in the query. Verify: 1) Column data types match the values being inserted/compared, 2) CAST functions are used for type conversions, 3) Schema definitions are correct, or 4) Date/time formats match expected patterns."
    },
    {
        "name": "test_code_57_success",
        "error_code": 57,
        "error_message": "Table already exists in the database. If you want to recreate it, first drop the existing table with: DROP TABLE table_name. If you want to modify the schema, use ALTER TABLE instead."
    },
    {
        "name": "test_code_60_success",
        "error_code": 60,
        "error_message": "Memory limit exceeded while processing the request. This typically occurs when working with large datasets. Try: 1) Add LIMIT clauses to reduce result size, 2) Use sampling (SAMPLE clause) for analysis, 3) Increase max_memory_usage setting, or 4) Optimize query with better filtering."
    },
    {
        "name": "test_code_62_success",
        "error_code": 62,
        "error_message": "Query size limit exceeded. The SQL statement is too large to process. Try: 1) Break complex queries into smaller operations, 2) Use temporary tables for intermediate results, 3) Increase max_query_size setting, or 4) Simplify the query structure."
    },
    {
        "name": "test_code_81_success",
        "error_code": 81,
        "error_message": "Database does not exist. Deploy schemas first using build-schemas and apply-schemas commands before running updates. Check: 1) Database name spelling, 2) Schema deployment completed successfully, or 3) Correct connection string."
    },
    {
        "name": "test_code_107_success",
        "error_code": 107,
        "error_message": "Table already exists in the database. If you want to recreate it, first drop the existing table with: DROP TABLE table_name. If you want to modify the schema, use ALTER TABLE instead."
    },
    {
        "name": "test_code_158_success",
        "error_code": 158,
        "error_message": "Too many parameters in the query. This usually happens with large IN clauses or prepared statements. Try: 1) Use temporary tables for large parameter lists, 2) Break queries into smaller operations, 3) Increase max_query_parameters setting, or 4) Use JOINs instead of large IN clauses."
    },
    {
        "name": "test_code_159_success",
        "error_code": 159,
        "error_message": "Query execution timed out. The operation took too long to complete. Try: 1) Add indexes on frequently filtered columns, 2) Use OPTIMIZE TABLE for better performance, 3) Increase timeout settings (max_execution_time), or 4) Break the query into smaller chunks."
    },
    {
        "name": "test_code_164_success",
        "error_code": 164,
        "error_message": "Unknown or invalid setting specified. Check: 1) Setting name spelling, 2) Setting exists in ClickHouse (check documentation), 3) Setting is available in your ClickHouse version, or 4) Correct syntax for setting values."
    },
    {
        "name": "test_code_173_success",
        "error_code": 173,
        "error_message": "Too many simultaneous queries running. The server is overloaded. Try: 1) Wait a few minutes and retry, 2) Reduce concurrent operations, 3) Check for long-running queries blocking others, or 4) Increase max_concurrent_queries setting."
    },
    {
        "name": "test_code_192_success",
        "error_code": 192,
        "error_message": "Access denied due to insufficient permissions. Check: 1) User has required privileges (GRANT statements), 2) Correct user credentials, 3) Database/table permissions, or 4) RBAC roles are properly assigned."
    },
    {
        "name": "test_code_193_success",
        "error_code": 193,
        "error_message": "Authentication failed. Verify: 1) Username and password are correct, 2) User account exists and is not locked, 3) Connection uses proper authentication method, or 4) SSL/TLS certificates are valid (if required)."
    },
    {
        "name": "test_code_209_success",
        "error_code": 209,
        "error_message": "Connection attempt timed out. Check: 1) ClickHouse server is running and accessible, 2) Network connectivity to the host/port, 3) Firewall rules allow connections, 4) Increase connection_timeout setting if needed."
    },
    {
        "name": "test_code_210_success",
        "error_code": 210,
        "error_message": "Connection to database was lost during operation. This can happen due to: 1) Network instability, 2) Server restart, 3) Connection timeout, or 4) Firewall blocking. Try reconnecting and ensure network stability."
    },
    {
        "name": "test_code_241_success",
        "error_code": 241,
        "error_message": "Memory limit exceeded for this specific query. Try: 1) Simplify the query with fewer JOINs or aggregations, 2) Use GROUP BY with LIMIT for large result sets, 3) Increase max_memory_usage_for_user setting, or 4) Consider using external aggregation for very large datasets."
    },
    {
        "name": "test_code_253_success",
        "error_code": 253,
        "error_message": "Replica is in readonly mode and cannot accept write operations. This is normal for: 1) Read replicas in distributed setups, 2) During maintenance windows, or 3) When replication lag is too high. Direct writes to the primary/master node instead."
    },
    {
        "name": "test_code_390_success",
        "error_code": 390,
        "error_message": "Table does not exist in the database. Ensure schemas are deployed first: run build-schemas then apply-schemas before attempting updates or queries. Check: 1) Table name spelling and case sensitivity, 2) Correct database selection, or 3) Schema deployment completed successfully."
    },
    {
        "name": "test_non_mapped_code_success",
        "error_code": 500,
        "error_message": None
    },
    {
        "name": "test_string_error_code_fail",
        "error_code": "36",
        "raises": {
            "exception": ValidationError,
            "message": "[type=int_type, input_value='36', input_type=str]"
        }
    },
    {
        "name": "test_float_error_code_fail",
        "error_code": 36.1,
        "raises": {
            "exception": ValidationError,
            "message": "[type=int_type, input_value=36.1, input_type=float]"
        }
    },
    {
        "name": "test_bool_error_code_fail",
        "error_code": True,
        "raises": {
            "exception": ValidationError,
            "message": "[type=int_type, input_value=True, input_type=bool]"
        }
    },
    {
        "name": "test_none_error_code_fail",
        "error_code": None,
        "raises": {
            "exception": ValidationError,
            "message": "[type=int_type, input_value=None, input_type=NoneType]"
        }
    }
]

TEST_CLICKHOUSE_ERRORS_MAPPING_GET_ERROR_TYPE = [
    {
        "name": "test_code_36_success",
        "error_code": 36,
        "error_type": "unknown_table"
    },
    {
        "name": "test_code_43_success",
        "error_code": 43,
        "error_type": "function_not_found"
    },
    {
        "name": "test_code_53_success",
        "error_code": 53,
        "error_type": "type_mismatch"
    },
    {
        "name": "test_code_57_success",
        "error_code": 57,
        "error_type": "table_already_exists"
    },
    {
        "name": "test_code_60_success",
        "error_code": 60,
        "error_type": "memory_limit"
    },
    {
        "name": "test_code_62_success",
        "error_code": 62,
        "error_type": "query_size_limit"
    },
    {
        "name": "test_code_81_success",
        "error_code": 81,
        "error_type": "database_not_found"
    },
    {
        "name": "test_code_107_success",
        "error_code": 107,
        "error_type": "table_already_exists"
    },
    {
        "name": "test_code_158_success",
        "error_code": 158,
        "error_type": "too_many_parameters"
    },
    {
        "name": "test_code_159_success",
        "error_code": 159,
        "error_type": "timeout"
    },
    {
        "name": "test_code_164_success",
        "error_code": 164,
        "error_type": "unknown_setting"
    },
    {
        "name": "test_code_173_success",
        "error_code": 173,
        "error_type": "too_many_queries"
    },
    {
        "name": "test_code_192_success",
        "error_code": 192,
        "error_type": "access_denied"
    },
    {
        "name": "test_code_193_success",
        "error_code": 193,
        "error_type": "authentication_failed"
    },
    {
        "name": "test_code_209_success",
        "error_code": 209,
        "error_type": "connection_timeout"
    },
    {
        "name": "test_code_210_success",
        "error_code": 210,
        "error_type": "connection_lost"
    },
    {
        "name": "test_code_241_success",
        "error_code": 241,
        "error_type": "memory_limit_query"
    },
    {
        "name": "test_code_253_success",
        "error_code": 253,
        "error_type": "replica_readonly"
    },
    {
        "name": "test_code_390_success",
        "error_code": 390,
        "error_type": "table_not_found"
    },
    {
        "name": "test_non_mapped_code_success",
        "error_code": 500,
        "error_type": "unknown_error"
    },
    {
        "name": "test_string_error_code_fail",
        "error_code": "36",
        "raises": {
            "exception": ValidationError,
            "message": "[type=int_type, input_value='36', input_type=str]"
        }
    },
    {
        "name": "test_float_error_code_fail",
        "error_code": 36.1,
        "raises": {
            "exception": ValidationError,
            "message": "[type=int_type, input_value=36.1, input_type=float]"
        }
    },
    {
        "name": "test_bool_error_code_fail",
        "error_code": True,
        "raises": {
            "exception": ValidationError,
            "message": "[type=int_type, input_value=True, input_type=bool]"
        }
    },
    {
        "name": "test_none_error_code_fail",
        "error_code": None,
        "raises": {
            "exception": ValidationError,
            "message": "[type=int_type, input_value=None, input_type=NoneType]"
        }
    }
]

TEST_CLICKHOUSE_ERRORS_MAPPING_GET_ERROR_CATEGORY = [
    {
        "name": "test_code_36_success",
        "error_code": 36,
        "error_category": "syntax"
    },
    {
        "name": "test_code_43_success",
        "error_code": 43,
        "error_category": "syntax"
    },
    {
        "name": "test_code_53_success",
        "error_code": 53,
        "error_category": "data"
    },
    {
        "name": "test_code_57_success",
        "error_code": 57,
        "error_category": "exists"
    },
    {
        "name": "test_code_60_success",
        "error_code": 60,
        "error_category": "resource_limit"
    },
    {
        "name": "test_code_62_success",
        "error_code": 62,
        "error_category": "query_limit"
    },
    {
        "name": "test_code_81_success",
        "error_code": 81,
        "error_category": "not_found"
    },
    {
        "name": "test_code_107_success",
        "error_code": 107,
        "error_category": "exists"
    },
    {
        "name": "test_code_158_success",
        "error_code": 158,
        "error_category": "resource_limit"
    },
    {
        "name": "test_code_159_success",
        "error_code": 159,
        "error_category": "timeout"
    },
    {
        "name": "test_code_164_success",
        "error_code": 164,
        "error_category": "configuration"
    },
    {
        "name": "test_code_173_success",
        "error_code": 173,
        "error_category": "concurrency"
    },
    {
        "name": "test_code_192_success",
        "error_code": 192,
        "error_category": "permission"
    },
    {
        "name": "test_code_193_success",
        "error_code": 193,
        "error_category": "authentication"
    },
    {
        "name": "test_code_209_success",
        "error_code": 209,
        "error_category": "connection"
    },
    {
        "name": "test_code_210_success",
        "error_code": 210,
        "error_category": "connection"
    },
    {
        "name": "test_code_241_success",
        "error_code": 241,
        "error_category": "resource_limit"
    },
    {
        "name": "test_code_253_success",
        "error_code": 253,
        "error_category": "replication"
    },
    {
        "name": "test_code_390_success",
        "error_code": 390,
        "error_category": "not_found"
    },
    {
        "name": "test_non_mapped_code_success",
        "error_code": 500,
        "error_category": None
    },
    {
        "name": "test_string_error_code_fail",
        "error_code": "36",
        "raises": {
            "exception": ValidationError,
            "message": "[type=int_type, input_value='36', input_type=str]"
        }
    },
    {
        "name": "test_float_error_code_fail",
        "error_code": 36.1,
        "raises": {
            "exception": ValidationError,
            "message": "[type=int_type, input_value=36.1, input_type=float]"
        }
    },
    {
        "name": "test_bool_error_code_fail",
        "error_code": True,
        "raises": {
            "exception": ValidationError,
            "message": "[type=int_type, input_value=True, input_type=bool]"
        }
    },
    {
        "name": "test_none_error_code_fail",
        "error_code": None,
        "raises": {
            "exception": ValidationError,
            "message": "[type=int_type, input_value=None, input_type=NoneType]"
        }
    }
]

TEST_CLICKHOUSE_ERRORS_MAPPING_IS_RESOURCE_LIMIT_ERROR = [
    {
        "name": "test_code_36_success",
        "error_code": 36,
        "is_resource_limit_error": False
    },
    {
        "name": "test_code_43_success",
        "error_code": 43,
        "is_resource_limit_error": False
    },
    {
        "name": "test_code_53_success",
        "error_code": 53,
        "is_resource_limit_error": False
    },
    {
        "name": "test_code_57_success",
        "error_code": 57,
        "is_resource_limit_error": False
    },
    {
        "name": "test_code_60_success",
        "error_code": 60,
        "is_resource_limit_error": True
    },
    {
        "name": "test_code_62_success",
        "error_code": 62,
        "is_resource_limit_error": False
    },
    {
        "name": "test_code_81_success",
        "error_code": 81,
        "is_resource_limit_error": False
    },
    {
        "name": "test_code_107_success",
        "error_code": 107,
        "is_resource_limit_error": False
    },
    {
        "name": "test_code_158_success",
        "error_code": 158,
        "is_resource_limit_error": True
    },
    {
        "name": "test_code_159_success",
        "error_code": 159,
        "is_resource_limit_error": False
    },
    {
        "name": "test_code_164_success",
        "error_code": 164,
        "is_resource_limit_error": False
    },
    {
        "name": "test_code_173_success",
        "error_code": 173,
        "is_resource_limit_error": False
    },
    {
        "name": "test_code_192_success",
        "error_code": 192,
        "is_resource_limit_error": False
    },
    {
        "name": "test_code_193_success",
        "error_code": 193,
        "is_resource_limit_error": False
    },
    {
        "name": "test_code_209_success",
        "error_code": 209,
        "is_resource_limit_error": False
    },
    {
        "name": "test_code_210_success",
        "error_code": 210,
        "is_resource_limit_error": False
    },
    {
        "name": "test_code_241_success",
        "error_code": 241,
        "is_resource_limit_error": True
    },
    {
        "name": "test_code_253_success",
        "error_code": 253,
        "is_resource_limit_error": False
    },
    {
        "name": "test_code_390_success",
        "error_code": 390,
        "is_resource_limit_error": False
    },
    {
        "name": "test_non_mapped_code_success",
        "error_code": 500,
        "is_resource_limit_error": False
    },
    {
        "name": "test_string_error_code_fail",
        "error_code": "36",
        "raises": {
            "exception": ValidationError,
            "message": "[type=int_type, input_value='36', input_type=str]"
        }
    },
    {
        "name": "test_float_error_code_fail",
        "error_code": 36.1,
        "raises": {
            "exception": ValidationError,
            "message": "[type=int_type, input_value=36.1, input_type=float]"
        }
    },
    {
        "name": "test_bool_error_code_fail",
        "error_code": True,
        "raises": {
            "exception": ValidationError,
            "message": "[type=int_type, input_value=True, input_type=bool]"
        }
    },
    {
        "name": "test_none_error_code_fail",
        "error_code": None,
        "raises": {
            "exception": ValidationError,
            "message": "[type=int_type, input_value=None, input_type=NoneType]"
        }
    }
]

TEST_CLICKHOUSE_ERRORS_MAPPING_IS_RETRYABLE_ERROR = [
    {
        "name": "test_code_36_success",
        "error_code": 36,
        "is_retryable_error": False
    },
    {
        "name": "test_code_43_success",
        "error_code": 43,
        "is_retryable_error": False
    },
    {
        "name": "test_code_53_success",
        "error_code": 53,
        "is_retryable_error": False
    },
    {
        "name": "test_code_57_success",
        "error_code": 57,
        "is_retryable_error": False
    },
    {
        "name": "test_code_60_success",
        "error_code": 60,
        "is_retryable_error": True
    },
    {
        "name": "test_code_62_success",
        "error_code": 62,
        "is_retryable_error": False
    },
    {
        "name": "test_code_81_success",
        "error_code": 81,
        "is_retryable_error": False
    },
    {
        "name": "test_code_107_success",
        "error_code": 107,
        "is_retryable_error": False
    },
    {
        "name": "test_code_158_success",
        "error_code": 158,
        "is_retryable_error": True
    },
    {
        "name": "test_code_159_success",
        "error_code": 159,
        "is_retryable_error": True
    },
    {
        "name": "test_code_164_success",
        "error_code": 164,
        "is_retryable_error": False
    },
    {
        "name": "test_code_173_success",
        "error_code": 173,
        "is_retryable_error": True
    },
    {
        "name": "test_code_192_success",
        "error_code": 192,
        "is_retryable_error": False
    },
    {
        "name": "test_code_193_success",
        "error_code": 193,
        "is_retryable_error": False
    },
    {
        "name": "test_code_209_success",
        "error_code": 209,
        "is_retryable_error": True
    },
    {
        "name": "test_code_210_success",
        "error_code": 210,
        "is_retryable_error": True
    },
    {
        "name": "test_code_241_success",
        "error_code": 241,
        "is_retryable_error": True
    },
    {
        "name": "test_code_253_success",
        "error_code": 253,
        "is_retryable_error": False
    },
    {
        "name": "test_code_390_success",
        "error_code": 390,
        "is_retryable_error": False
    },
    {
        "name": "test_non_mapped_code_success",
        "error_code": 500,
        "is_retryable_error": False
    },
    {
        "name": "test_string_error_code_fail",
        "error_code": "36",
        "raises": {
            "exception": ValidationError,
            "message": "[type=int_type, input_value='36', input_type=str]"
        }
    },
    {
        "name": "test_float_error_code_fail",
        "error_code": 36.1,
        "raises": {
            "exception": ValidationError,
            "message": "[type=int_type, input_value=36.1, input_type=float]"
        }
    },
    {
        "name": "test_bool_error_code_fail",
        "error_code": True,
        "raises": {
            "exception": ValidationError,
            "message": "[type=int_type, input_value=True, input_type=bool]"
        }
    },
    {
        "name": "test_none_error_code_fail",
        "error_code": None,
        "raises": {
            "exception": ValidationError,
            "message": "[type=int_type, input_value=None, input_type=NoneType]"
        }
    }
]

TEST_CLICKHOUSE_ERRORS_MAPPING_FORMAT_ERROR_FOR_API = [
    {
        "name": "test_code_36_success",
        "error": "Code: 36. DB::Exception: Table default.non_existent_table doesn't exist. (UNKNOWN_TABLE)",
        "expected_dict": {
            "error": {
                "code": 36,
                "category": "syntax",
                "type": "unknown_table",
                "message": "Code: 36. DB::Exception: Table default.non_existent_table doesn't exist. (UNKNOWN_TABLE)",
                "user_message": "Unknown table referenced in the query. Verify: 1) Table name is spelled correctly, 2) Table exists in the current database, 3) Correct database is selected (USE database_name), or 4) Table permissions allow access.",
                "is_actionable": True
            }
        }
    },
    {
        "name": "test_code_43_success",
        "error": "Code: 43. DB::Exception: Unknown function fakeFunction. (UNKNOWN_FUNCTION)",
        "expected_dict": {
            "error": {
                "code": 43,
                "category": "syntax",
                "type": "function_not_found",
                "message": "Code: 43. DB::Exception: Unknown function fakeFunction. (UNKNOWN_FUNCTION)",
                "user_message": "Function not found in the query. Check: 1) Function name spelling, 2) Function exists in ClickHouse (check documentation), 3) Required parameters are provided, or 4) Custom functions are properly installed.",
                "is_actionable": True
            }
        }
    },
    {
        "name": "test_code_53_success",
        "error": "Code: 53. DB::Exception: Type mismatch in IN or VALUES section. Expected: UInt64. Got: String. (TYPE_MISMATCH)",
        "expected_dict": {
            "error": {
                "code": 53,
                "category": "data",
                "type": "type_mismatch",
                "message": "Code: 53. DB::Exception: Type mismatch in IN or VALUES section. Expected: UInt64. Got: String. (TYPE_MISMATCH)",
                "user_message": "Data type mismatch in the query. Verify: 1) Column data types match the values being inserted/compared, 2) CAST functions are used for type conversions, 3) Schema definitions are correct, or 4) Date/time formats match expected patterns.",
                "is_actionable": True
            }
        }
    },
    {
        "name": "test_code_57_success",
        "error": "Code: 57. DB::Exception: Table default.users already exists. (TABLE_ALREADY_EXISTS)",
        "expected_dict": {
            "error": {
                "code": 57,
                "category": "exists",
                "type": "table_already_exists",
                "message": "Code: 57. DB::Exception: Table default.users already exists. (TABLE_ALREADY_EXISTS)",
                "user_message": "Table already exists in the database. If you want to recreate it, first drop the existing table with: DROP TABLE table_name. If you want to modify the schema, use ALTER TABLE instead.",
                "is_actionable": True
            }
        }
    },
    {
        "name": "test_code_60_success",
        "error": "Code: 60. DB::Exception: Memory limit (for query) exceeded: would use 10.00 GiB (attempt to allocate chunk of 134217728 bytes), maximum: 9.31 GiB. (MEMORY_LIMIT_EXCEEDED)",
        "expected_dict": {
            "error": {
                "code": 60,
                "category": "resource_limit",
                "type": "memory_limit",
                "message": "Code: 60. DB::Exception: Memory limit (for query) exceeded: would use 10.00 GiB (attempt to allocate chunk of 134217728 bytes), maximum: 9.31 GiB. (MEMORY_LIMIT_EXCEEDED)",
                "user_message": "Memory limit exceeded while processing the request. This typically occurs when working with large datasets. Try: 1) Add LIMIT clauses to reduce result size, 2) Use sampling (SAMPLE clause) for analysis, 3) Increase max_memory_usage setting, or 4) Optimize query with better filtering.",
                "is_actionable": True
            }
        }
    },
    {
        "name": "test_code_62_success",
        "error": "Code: 62. DB::Exception: Query is too large. Maximum allowed query size is 262144 bytes. (QUERY_IS_TOO_BIG)",
        "expected_dict": {
            "error": {
                "code": 62,
                "category": "query_limit",
                "type": "query_size_limit",
                "message": "Code: 62. DB::Exception: Query is too large. Maximum allowed query size is 262144 bytes. (QUERY_IS_TOO_BIG)",
                "user_message": "Query size limit exceeded. The SQL statement is too large to process. Try: 1) Break complex queries into smaller operations, 2) Use temporary tables for intermediate results, 3) Increase max_query_size setting, or 4) Simplify the query structure.",
                "is_actionable": True
            }
        }
    },
    {
        "name": "test_code_81_success",
        "error": "Code: 81. DB::Exception: Database analytics doesn't exist. (UNKNOWN_DATABASE)",
        "expected_dict": {
            "error": {
                "code": 81,
                "category": "not_found",
                "type": "database_not_found",
                "message": "Code: 81. DB::Exception: Database analytics doesn't exist. (UNKNOWN_DATABASE)",
                "user_message": "Database does not exist. Deploy schemas first using build-schemas and apply-schemas commands before running updates. Check: 1) Database name spelling, 2) Schema deployment completed successfully, or 3) Correct connection string.",
                "is_actionable": True
            }
        }
    },
    {
        "name": "test_code_107_success",
        "error": "Code: 107. DB::Exception: Table default.events already exists. (TABLE_ALREADY_EXISTS)",
        "expected_dict": {
            "error": {
                "code": 107,
                "category": "exists",
                "type": "table_already_exists",
                "message": "Code: 107. DB::Exception: Table default.events already exists. (TABLE_ALREADY_EXISTS)",
                "user_message": "Table already exists in the database. If you want to recreate it, first drop the existing table with: DROP TABLE table_name. If you want to modify the schema, use ALTER TABLE instead.",
                "is_actionable": True
            }
        }
    },
    {
        "name": "test_code_158_success",
        "error": "Code: 158. DB::Exception: Too many parameters: 100001. Maximum: 100000. (TOO_MANY_QUERY_PARAMETERS)",
        "expected_dict": {
            "error": {
                "code": 158,
                "category": "resource_limit",
                "type": "too_many_parameters",
                "message": "Code: 158. DB::Exception: Too many parameters: 100001. Maximum: 100000. (TOO_MANY_QUERY_PARAMETERS)",
                "user_message": "Too many parameters in the query. This usually happens with large IN clauses or prepared statements. Try: 1) Use temporary tables for large parameter lists, 2) Break queries into smaller operations, 3) Increase max_query_parameters setting, or 4) Use JOINs instead of large IN clauses.",
                "is_actionable": True
            }
        }
    },
    {
        "name": "test_code_159_success",
        "error": "Code: 159. DB::Exception: Timeout exceeded: elapsed 30.5 seconds, maximum: 30. (TIMEOUT_EXCEEDED)",
        "expected_dict": {
            "error": {
                "code": 159,
                "category": "timeout",
                "type": "timeout",
                "message": "Code: 159. DB::Exception: Timeout exceeded: elapsed 30.5 seconds, maximum: 30. (TIMEOUT_EXCEEDED)",
                "user_message": "Query execution timed out. The operation took too long to complete. Try: 1) Add indexes on frequently filtered columns, 2) Use OPTIMIZE TABLE for better performance, 3) Increase timeout settings (max_execution_time), or 4) Break the query into smaller chunks.",
                "is_actionable": True
            }
        }
    },
    {
        "name": "test_code_164_success",
        "error": "Code: 164. DB::Exception: Unknown setting invalid_setting_name. (UNKNOWN_SETTING)",
        "expected_dict": {
            "error": {
                "code": 164,
                "category": "configuration",
                "type": "unknown_setting",
                "message": "Code: 164. DB::Exception: Unknown setting invalid_setting_name. (UNKNOWN_SETTING)",
                "user_message": "Unknown or invalid setting specified. Check: 1) Setting name spelling, 2) Setting exists in ClickHouse (check documentation), 3) Setting is available in your ClickHouse version, or 4) Correct syntax for setting values.",
                "is_actionable": True
            }
        }
    },
    {
        "name": "test_code_173_success",
        "error": "Code: 173. DB::Exception: Too many simultaneous queries. Maximum: 100. (TOO_MANY_SIMULTANEOUS_QUERIES)",
        "expected_dict": {
            "error": {
                "code": 173,
                "category": "concurrency",
                "type": "too_many_queries",
                "message": "Code: 173. DB::Exception: Too many simultaneous queries. Maximum: 100. (TOO_MANY_SIMULTANEOUS_QUERIES)",
                "user_message": "Too many simultaneous queries running. The server is overloaded. Try: 1) Wait a few minutes and retry, 2) Reduce concurrent operations, 3) Check for long-running queries blocking others, or 4) Increase max_concurrent_queries setting.",
                "is_actionable": True
            }
        }
    },
    {
        "name": "test_code_192_success",
        "error": "Code: 192. DB::Exception: user_readonly: Not enough privileges. To execute this query it's necessary to have grant SELECT ON default.sensitive_data. (ACCESS_DENIED)",
        "expected_dict": {
            "error": {
                "code": 192,
                "category": "permission",
                "type": "access_denied",
                "message": "Code: 192. DB::Exception: user_readonly: Not enough privileges. To execute this query it's necessary to have grant SELECT ON default.sensitive_data. (ACCESS_DENIED)",
                "user_message": "Access denied due to insufficient permissions. Check: 1) User has required privileges (GRANT statements), 2) Correct user credentials, 3) Database/table permissions, or 4) RBAC roles are properly assigned.",
                "is_actionable": True
            }
        }
    },
    {
        "name": "test_code_193_success",
        "error": "Code: 193. DB::Exception: Authentication failed: password is incorrect or there is no user with such name. (AUTHENTICATION_FAILED)",
        "expected_dict": {
            "error": {
                "code": 193,
                "category": "authentication",
                "type": "authentication_failed",
                "message": "Code: 193. DB::Exception: Authentication failed: password is incorrect or there is no user with such name. (AUTHENTICATION_FAILED)",
                "user_message": "Authentication failed. Verify: 1) Username and password are correct, 2) User account exists and is not locked, 3) Connection uses proper authentication method, or 4) SSL/TLS certificates are valid (if required).",
                "is_actionable": True
            }
        }
    },
    {
        "name": "test_code_209_success",
        "error": "Code: 209. DB::NetException: Connection timeout: connect timed out after 10000ms. (SOCKET_TIMEOUT)",
        "expected_dict": {
            "error": {
                "code": 209,
                "category": "connection",
                "type": "connection_timeout",
                "message": "Code: 209. DB::NetException: Connection timeout: connect timed out after 10000ms. (SOCKET_TIMEOUT)",
                "user_message": "Connection attempt timed out. Check: 1) ClickHouse server is running and accessible, 2) Network connectivity to the host/port, 3) Firewall rules allow connections, 4) Increase connection_timeout setting if needed.",
                "is_actionable": True
            }
        }
    },
    {
        "name": "test_code_210_success",
        "error": "Code: 210. DB::NetException: Connection reset by peer, while reading from socket. (NETWORK_ERROR)",
        "expected_dict": {
            "error": {
                "code": 210,
                "category": "connection",
                "type": "connection_lost",
                "message": "Code: 210. DB::NetException: Connection reset by peer, while reading from socket. (NETWORK_ERROR)",
                "user_message": "Connection to database was lost during operation. This can happen due to: 1) Network instability, 2) Server restart, 3) Connection timeout, or 4) Firewall blocking. Try reconnecting and ensure network stability.",
                "is_actionable": True
            }
        }
    },
    {
        "name": "test_code_241_success",
        "error": "Code: 241. DB::Exception: Memory limit (for user) exceeded: would use 15.00 GiB, maximum: 10.00 GiB. (MEMORY_LIMIT_EXCEEDED)",
        "expected_dict": {
            "error": {
                "code": 241,
                "category": "resource_limit",
                "type": "memory_limit_query",
                "message": "Code: 241. DB::Exception: Memory limit (for user) exceeded: would use 15.00 GiB, maximum: 10.00 GiB. (MEMORY_LIMIT_EXCEEDED)",
                "user_message": "Memory limit exceeded for this specific query. Try: 1) Simplify the query with fewer JOINs or aggregations, 2) Use GROUP BY with LIMIT for large result sets, 3) Increase max_memory_usage_for_user setting, or 4) Consider using external aggregation for very large datasets.",
                "is_actionable": True
            }
        }
    },
    {
        "name": "test_code_253_success",
        "error": "Code: 253. DB::Exception: Table is in readonly mode since replica is not leader. (TABLE_IS_READ_ONLY)",
        "expected_dict": {
            "error": {
                "code": 253,
                "category": "replication",
                "type": "replica_readonly",
                "message": "Code: 253. DB::Exception: Table is in readonly mode since replica is not leader. (TABLE_IS_READ_ONLY)",
                "user_message": "Replica is in readonly mode and cannot accept write operations. This is normal for: 1) Read replicas in distributed setups, 2) During maintenance windows, or 3) When replication lag is too high. Direct writes to the primary/master node instead.",
                "is_actionable": True
            }
        }
    },
    {
        "name": "test_code_390_success",
        "error": "Code: 390. DB::Exception: Table default.missing_table doesn't exist. (UNKNOWN_TABLE)",
        "expected_dict": {
            "error": {
                "code": 390,
                "category": "not_found",
                "type": "table_not_found",
                "message": "Code: 390. DB::Exception: Table default.missing_table doesn't exist. (UNKNOWN_TABLE)",
                "user_message": "Table does not exist in the database. Ensure schemas are deployed first: run build-schemas then apply-schemas before attempting updates or queries. Check: 1) Table name spelling and case sensitivity, 2) Correct database selection, or 3) Schema deployment completed successfully.",
                "is_actionable": True
            }
        }
    },
    {
        "name": "test_database_string_case_success",
        "error": "Code: XXX. DB::Exception: Database analytics does not exist. (UNKNOWN_DATABASE)",
        "expected_dict": {
            "error": {
                "code": 81,
                "category": "not_found",
                "type": "database_not_found",
                "message": "Code: XXX. DB::Exception: Database analytics does not exist. (UNKNOWN_DATABASE)",
                "user_message": "Database does not exist. Deploy schemas first using build-schemas and apply-schemas commands before running updates. Check: 1) Database name spelling, 2) Schema deployment completed successfully, or 3) Correct connection string.",
                "is_actionable": True
            }
        }
    },
    {
        "name": "test_unmatching_case_success",
        "error": "Code: XXX. DB::Exception: Table default.missing_table doesn't exist. (UNKNOWN_TABLE)",
        "expected_dict": {
            "error": {
                "code": None,
                "category": "unknown",
                "type": "unknown_error",
                "message": "Code: XXX. DB::Exception: Table default.missing_table doesn't exist. (UNKNOWN_TABLE)",
                "user_message": "Database operation failed. Ensure schemas are deployed first using apply-schemas command before running updates. Check server logs for technical details if the issue persists.",
                "is_actionable": False
            }
        }
    }
]

@pytest.fixture(params = TEST_CLICKHOUSE_ERRORS_MAPPING_PARSE_ERROR, ids = lambda x: x["name"])
def test_clickhouse_errors_mapping_parse_error(request):
    return request.param

@pytest.fixture(params = TEST_CLICKHOUSE_ERRORS_MAPPING_GET_ERROR_MESSAGE, ids = lambda x: x["name"])
def test_clickhouse_errors_mapping_get_error_message(request):
    return request.param

@pytest.fixture(params = TEST_CLICKHOUSE_ERRORS_MAPPING_GET_ERROR_TYPE, ids = lambda x: x["name"])
def test_clickhouse_errors_mapping_get_error_type(request):
    return request.param

@pytest.fixture(params = TEST_CLICKHOUSE_ERRORS_MAPPING_GET_ERROR_CATEGORY, ids = lambda x: x["name"])
def test_clickhouse_errors_mapping_get_error_category(request):
    return request.param

@pytest.fixture(params = TEST_CLICKHOUSE_ERRORS_MAPPING_IS_RESOURCE_LIMIT_ERROR, ids = lambda x: x["name"])
def test_clickhouse_errors_mapping_is_resource_limit_error(request):
    return request.param

@pytest.fixture(params = TEST_CLICKHOUSE_ERRORS_MAPPING_IS_RETRYABLE_ERROR, ids = lambda x: x["name"])
def test_clickhouse_errors_mapping_is_retryable_error(request):
    return request.param

@pytest.fixture(params = TEST_CLICKHOUSE_ERRORS_MAPPING_FORMAT_ERROR_FOR_API, ids = lambda x: x["name"])
def test_clickhouse_errors_mapping_format_error_for_api(request):
    return request.param