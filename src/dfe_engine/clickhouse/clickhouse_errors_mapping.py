import re
from typing import Dict, Optional


class ClickHouseErrorHandler:
    """
    Centralized error handling for ClickHouse database operations.
    Provides user-friendly error messages for common ClickHouse error codes.
    """

   
    ERROR_MAPPINGS = {
       
        60: {
            'type': 'memory_limit',
            'message': 'Memory limit exceeded while processing the request. This typically occurs when working with large datasets. Try: 1) Add LIMIT clauses to reduce result size, 2) Use sampling (SAMPLE clause) for analysis, 3) Increase max_memory_usage setting, or 4) Optimize query with better filtering.',
            'category': 'resource_limit'
        },
        62: {
            'type': 'query_size_limit',
            'message': 'Query size limit exceeded. The SQL statement is too large to process. Try: 1) Break complex queries into smaller operations, 2) Use temporary tables for intermediate results, 3) Increase max_query_size setting, or 4) Simplify the query structure.',
            'category': 'query_limit'
        },
        159: {
            'type': 'timeout',
            'message': 'Query execution timed out. The operation took too long to complete. Try: 1) Add indexes on frequently filtered columns, 2) Use OPTIMIZE TABLE for better performance, 3) Increase timeout settings (max_execution_time), or 4) Break the query into smaller chunks.',
            'category': 'timeout'
        },
        173: {
            'type': 'too_many_queries',
            'message': 'Too many simultaneous queries running. The server is overloaded. Try: 1) Wait a few minutes and retry, 2) Reduce concurrent operations, 3) Check for long-running queries blocking others, or 4) Increase max_concurrent_queries setting.',
            'category': 'concurrency'
        },
        241: {
            'type': 'memory_limit_query',
            'message': 'Memory limit exceeded for this specific query. Try: 1) Simplify the query with fewer JOINs or aggregations, 2) Use GROUP BY with LIMIT for large result sets, 3) Increase max_memory_usage_for_user setting, or 4) Consider using external aggregation for very large datasets.',
            'category': 'resource_limit'
        },

       
        57: {
            'type': 'table_already_exists',
            'message': 'Table already exists in the database. If you want to recreate it, first drop the existing table with: DROP TABLE table_name. If you want to modify the schema, use ALTER TABLE instead.',
            'category': 'exists'
        },
        390: {
            'type': 'table_not_found',
            'message': 'Table does not exist in the database. Ensure schemas are deployed first: run build-schemas then apply-schemas before attempting updates or queries. Check: 1) Table name spelling and case sensitivity, 2) Correct database selection, or 3) Schema deployment completed successfully.',
            'category': 'not_found'
        },
        81: {
            'type': 'database_not_found',
            'message': 'Database does not exist. Deploy schemas first using build-schemas and apply-schemas commands before running updates. Check: 1) Database name spelling, 2) Schema deployment completed successfully, or 3) Correct connection string.',
            'category': 'not_found'
        },

       
        36: {
            'type': 'unknown_table',
            'message': 'Unknown table referenced in the query. Verify: 1) Table name is spelled correctly, 2) Table exists in the current database, 3) Correct database is selected (USE database_name), or 4) Table permissions allow access.',
            'category': 'syntax'
        },
        43: {
            'type': 'function_not_found',
            'message': 'Function not found in the query. Check: 1) Function name spelling, 2) Function exists in ClickHouse (check documentation), 3) Required parameters are provided, or 4) Custom functions are properly installed.',
            'category': 'syntax'
        },
        53: {
            'type': 'type_mismatch',
            'message': 'Data type mismatch in the query. Verify: 1) Column data types match the values being inserted/compared, 2) CAST functions are used for type conversions, 3) Schema definitions are correct, or 4) Date/time formats match expected patterns.',
            'category': 'data'
        },
        158: {
            'type': 'too_many_parameters',
            'message': 'Too many parameters in the query. This usually happens with large IN clauses or prepared statements. Try: 1) Use temporary tables for large parameter lists, 2) Break queries into smaller operations, 3) Increase max_query_parameters setting, or 4) Use JOINs instead of large IN clauses.',
            'category': 'resource_limit'
        },
        164: {
            'type': 'unknown_setting',
            'message': 'Unknown or invalid setting specified. Check: 1) Setting name spelling, 2) Setting exists in ClickHouse (check documentation), 3) Setting is available in your ClickHouse version, or 4) Correct syntax for setting values.',
            'category': 'configuration'
        },
        107: {
            'type': 'table_already_exists',
            'message': 'Table already exists in the database. If you want to recreate it, first drop the existing table with: DROP TABLE table_name. If you want to modify the schema, use ALTER TABLE instead.',
            'category': 'exists'
        },
        253: {
            'type': 'replica_readonly',
            'message': 'Replica is in readonly mode and cannot accept write operations. This is normal for: 1) Read replicas in distributed setups, 2) During maintenance windows, or 3) When replication lag is too high. Direct writes to the primary/master node instead.',
            'category': 'replication'
        },

       
        192: {
            'type': 'access_denied',
            'message': 'Access denied due to insufficient permissions. Check: 1) User has required privileges (GRANT statements), 2) Correct user credentials, 3) Database/table permissions, or 4) RBAC roles are properly assigned.',
            'category': 'permission'
        },
        193: {
            'type': 'authentication_failed',
            'message': 'Authentication failed. Verify: 1) Username and password are correct, 2) User account exists and is not locked, 3) Connection uses proper authentication method, or 4) SSL/TLS certificates are valid (if required).',
            'category': 'authentication'
        },

       
        210: {
            'type': 'connection_lost',
            'message': 'Connection to database was lost during operation. This can happen due to: 1) Network instability, 2) Server restart, 3) Connection timeout, or 4) Firewall blocking. Try reconnecting and ensure network stability.',
            'category': 'connection'
        },
        209: {
            'type': 'connection_timeout',
            'message': 'Connection attempt timed out. Check: 1) ClickHouse server is running and accessible, 2) Network connectivity to the host/port, 3) Firewall rules allow connections, 4) Increase connection_timeout setting if needed.',
            'category': 'connection'
        }
    }

    @classmethod
    def parse_error(cls, error: Exception) -> Dict:
        """
        Parse a ClickHouse error and return structured error information.

        :param error: The exception from ClickHouse
        :return: Dict with 'code', 'type', 'message', 'user_message', 'actionable' keys
        """
        error_str = str(error)

       
        code_match = re.search(r'Code:\s*(\d+)', error_str)
        if code_match:
            error_code = int(code_match.group(1))
            if error_code in cls.ERROR_MAPPINGS:
                mapping = cls.ERROR_MAPPINGS[error_code]
                return {
                    'code': error_code,
                    'type': mapping['type'],
                    'message': error_str, 
                    'user_message': mapping['message'], 
                    'actionable': True 
                }

       
        if "Database" in error_str and "does not exist" in error_str:
            return {
                'code': 81, 
                'type': 'database_not_found',
                'message': error_str,
                'user_message': "Database does not exist. Deploy schemas first using build-schemas and apply-schemas commands before running updates. Check: 1) Database name spelling, 2) Schema deployment completed successfully, or 3) Correct connection string.",
                'actionable': True
            }

       
        return {
            'code': None,
            'type': 'unknown_error',
            'message': error_str, 
            'user_message': 'Database operation failed. Ensure schemas are deployed first using apply-schemas command before running updates. Check server logs for technical details if the issue persists.',
            'actionable': False 
        }

    @classmethod
    def get_error_message(cls, error_code: int) -> Optional[str]:
        """
        Get user-friendly error message for a specific error code.

        :param error_code: ClickHouse error code
        :return: User-friendly error message or None if code not found
        """
        if error_code in cls.ERROR_MAPPINGS:
            return cls.ERROR_MAPPINGS[error_code]['message']
        return None

    @classmethod
    def get_error_type(cls, error_code: int) -> Optional[str]:
        """
        Get error type for a specific error code.

        :param error_code: ClickHouse error code
        :return: Error type or 'unknown_error' if code not found
        """
        if error_code in cls.ERROR_MAPPINGS:
            return cls.ERROR_MAPPINGS[error_code]['type']
        return 'unknown_error'

    @classmethod
    def get_error_category(cls, error_code: int) -> Optional[str]:
        """
        Get error category for a specific error code.

        :param error_code: ClickHouse error code
        :return: Error category or None if code not found
        """
        if error_code in cls.ERROR_MAPPINGS:
            return cls.ERROR_MAPPINGS[error_code]['category']
        return None

    @classmethod
    def is_resource_limit_error(cls, error_code: int) -> bool:
        """
        Check if the error code represents a resource limit issue.

        :param error_code: ClickHouse error code
        :return: True if it's a resource limit error
        """
        return cls.get_error_category(error_code) == 'resource_limit'

    @classmethod
    def is_retryable_error(cls, error_code: int) -> bool:
        """
        Check if the error is retryable (transient errors).

        :param error_code: ClickHouse error code
        :return: True if the error is likely retryable
        """
        retryable_categories = ['connection', 'timeout', 'concurrency', 'resource_limit']
        category = cls.get_error_category(error_code)
        return category in retryable_categories

    @classmethod
    def format_error_for_api(cls, error: Exception) -> Dict:
        """
        Format a ClickHouse error for API response.

        :param error: The exception from ClickHouse
        :return: Dict formatted for API response
        """
        parsed = cls.parse_error(error)
        return {
            'error': {
                'code': parsed['code'],
                'type': parsed['type'],
                'message': parsed['user_message'],
                'details': parsed['message'],
                'actionable': parsed['actionable']
            }
        }