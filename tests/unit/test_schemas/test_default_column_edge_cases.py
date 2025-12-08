"""
Comprehensive edge case tests for default column handling in ClickHouse schema building.
Tests all scenarios: empty strings, zeros, nulls, functions, backward compatibility.
"""

import logging
import pandas as pd
import numpy as np
import pytest
from dfe_engine.schema.schema_util import SchemaUtils


class TestDefaultColumnEdgeCases:
    """Test edge cases for default column handling."""

    @pytest.fixture
    def logger(self):
        """Create a logger for tests."""
        return logging.getLogger(__name__)

    def generate_sql(self, df):
        """Simulate schema_ch.py logic for generating SQL."""
        sql_lines = []
        has_default_column = 'default' in df.columns

        for _, row in df.iterrows():
            column_default = ''
            if has_default_column and pd.notnull(row.get('default')) and str(row.get('default')).strip() != '':
                column_default = 'DEFAULT ' + str(row['default'])

            column_line = f"{row['column']} {row['type']}{' ' + column_default if column_default else ''}".strip()
            sql_lines.append(column_line)

        return sql_lines

    def test_clickhouse_empty_string_literal_with_default(self):
        """
        Test: Empty string as ClickHouse literal ''
        Expected: Should generate DEFAULT '' (because '' is the string literal value)
        """
        df = pd.DataFrame({
            'column': ['status'],
            'type': ['String'],
            'default': ["''"]  # ClickHouse string literal for empty string
        })

        sql_lines = self.generate_sql(df)
        
        # '' is a valid ClickHouse literal, so it SHOULD generate DEFAULT
        assert len(sql_lines) == 1
        assert "DEFAULT ''" in sql_lines[0]
        assert sql_lines[0] == "status String DEFAULT ''"

    def test_whitespace_only_no_default(self):
        """
        Test: Whitespace-only values (spaces, tabs, newlines)
        Expected: NO DEFAULT clause (whitespace is treated as empty)
        """
        df = pd.DataFrame({
            'column': ['description', 'note', 'comment'],
            'type': ['String', 'String', 'String'],
            'default': ['   ', '\t', '\n  \t\n']
        })

        sql_lines = self.generate_sql(df)

        # Whitespace-only should be treated as empty
        assert len(sql_lines) == 3
        for i, line in enumerate(sql_lines):
            assert 'DEFAULT' not in line, f"Line {i} should not have DEFAULT: {line}"
        assert sql_lines == [
            'description String',
            'note String',
            'comment String'
        ]

    def test_zero_values_with_default(self):
        """
        Test: Zero and zero-like values (0, 0.0)
        Expected: Should generate DEFAULT 0 (zero is a valid default)
        """
        df = pd.DataFrame({
            'column': ['count', 'score', 'rating'],
            'type': ['UInt32', 'Float64', 'Int32'],
            'default': ['0', '0.0', '0']
        })

        sql_lines = self.generate_sql(df)

        assert len(sql_lines) == 3
        assert sql_lines == [
            'count UInt32 DEFAULT 0',
            'score Float64 DEFAULT 0.0',
            'rating Int32 DEFAULT 0'
        ]
        for line in sql_lines:
            assert 'DEFAULT' in line

    def test_none_null_values_no_default(self):
        """
        Test: None, np.nan, pd.NA values
        Expected: NO DEFAULT clause (nulls are filtered by pd.notnull())
        """
        df = pd.DataFrame({
            'column': ['optional', 'nullable', 'missing'],
            'type': ['String', 'String', 'String'],
            'default': [None, np.nan, pd.NA]
        })

        sql_lines = self.generate_sql(df)

        assert len(sql_lines) == 3
        for i, line in enumerate(sql_lines):
            assert 'DEFAULT' not in line, f"Line {i} should not have DEFAULT: {line}"
        assert sql_lines == [
            'optional String',
            'nullable String',
            'missing String'
        ]

    def test_clickhouse_functions_with_default(self):
        """
        Test: Valid ClickHouse function calls
        Expected: Should generate DEFAULT with function calls
        """
        df = pd.DataFrame({
            'column': ['created_at', 'id', 'updated_at'],
            'type': ['DateTime', 'String', 'DateTime'],
            'default': ['now()', 'generateUUIDv4()', 'now()']
        })

        sql_lines = self.generate_sql(df)

        assert len(sql_lines) == 3
        assert sql_lines == [
            'created_at DateTime DEFAULT now()',
            'id String DEFAULT generateUUIDv4()',
            'updated_at DateTime DEFAULT now()'
        ]

    def test_string_literals_with_quotes_with_default(self):
        """
        Test: String literals with quotes
        Expected: Should preserve the quotes and generate DEFAULT
        """
        df = pd.DataFrame({
            'column': ['status', 'priority', 'level'],
            'type': ['String', 'String', 'String'],
            'default': ["'active'", "'normal'", "'info'"]
        })

        sql_lines = self.generate_sql(df)

        assert len(sql_lines) == 3
        assert sql_lines == [
            "status String DEFAULT 'active'",
            "priority String DEFAULT 'normal'",
            "level String DEFAULT 'info'"
        ]

    def test_mixed_realistic_scenario(self):
        """
        Test: Real-world scenario with mixed default values
        Expected: Only non-empty values generate DEFAULT clauses
        """
        df = pd.DataFrame({
            'column': ['id', 'name', 'email', 'created_at', 'status', 'retry_count', 'description'],
            'type': ['String', 'String', 'String', 'DateTime', 'String', 'UInt32', 'String'],
            'default': ["generateUUIDv4()", "'N/A'", '', 'now()', "'active'", '0', None]
        })

        sql_lines = self.generate_sql(df)

        assert len(sql_lines) == 7
        assert sql_lines == [
            'id String DEFAULT generateUUIDv4()',
            "name String DEFAULT 'N/A'",
            'email String',  # Empty string → no DEFAULT
            'created_at DateTime DEFAULT now()',
            "status String DEFAULT 'active'",
            'retry_count UInt32 DEFAULT 0',
            'description String'  # None → no DEFAULT
        ]

    def test_backward_compatibility_no_default_column(self):
        """
        Test: DataFrames without 'default' column (old schema)
        Expected: Should work without errors, generating clean DDL
        """
        df = pd.DataFrame({
            'column': ['event_id', 'timestamp', 'status'],
            'type': ['String', 'DateTime', 'String'],
            'index_type': ['', '', '']
        })

        sql_lines = self.generate_sql(df)

        assert len(sql_lines) == 3
        assert sql_lines == [
            'event_id String',
            'timestamp DateTime',
            'status String'
        ]
        # Backward compatible: no errors, clean DDL
        for line in sql_lines:
            assert 'DEFAULT' not in line

    def test_boolean_values_with_default(self):
        """
        Test: Boolean-like values (0, 1)
        Expected: Should generate DEFAULT with numeric values
        """
        df = pd.DataFrame({
            'column': ['is_active', 'is_deleted', 'is_verified'],
            'type': ['UInt8', 'UInt8', 'UInt8'],
            'default': ['1', '0', '1']
        })

        sql_lines = self.generate_sql(df)

        assert len(sql_lines) == 3
        assert sql_lines == [
            'is_active UInt8 DEFAULT 1',
            'is_deleted UInt8 DEFAULT 0',
            'is_verified UInt8 DEFAULT 1'
        ]

    def test_numeric_strings_with_default(self):
        """
        Test: Numeric values as strings
        Expected: Should generate DEFAULT with numeric values
        """
        df = pd.DataFrame({
            'column': ['count', 'age', 'port', 'timeout'],
            'type': ['UInt32', 'UInt8', 'UInt16', 'UInt32'],
            'default': ['100', '25', '8080', '3600']
        })

        sql_lines = self.generate_sql(df)

        assert len(sql_lines) == 4
        assert sql_lines == [
            'count UInt32 DEFAULT 100',
            'age UInt8 DEFAULT 25',
            'port UInt16 DEFAULT 8080',
            'timeout UInt32 DEFAULT 3600'
        ]

    def test_negative_numbers_with_default(self):
        """
        Test: Negative numeric values
        Expected: Should generate DEFAULT with negative values
        """
        df = pd.DataFrame({
            'column': ['offset', 'delta', 'adjustment'],
            'type': ['Int32', 'Int32', 'Float64'],
            'default': ['-1', '-100', '-0.5']
        })

        sql_lines = self.generate_sql(df)

        assert len(sql_lines) == 3
        assert sql_lines == [
            'offset Int32 DEFAULT -1',
            'delta Int32 DEFAULT -100',
            'adjustment Float64 DEFAULT -0.5'
        ]

    def test_complex_expressions_with_default(self):
        """
        Test: Complex ClickHouse expressions
        Expected: Should preserve complex expressions
        """
        df = pd.DataFrame({
            'column': ['timestamp_loaded', 'computed_value', 'complex_expr'],
            'type': ['DateTime', 'UInt32', 'String'],
            'default': ['toDateTime(now())', "toUInt32(rand())", "concat('prefix_', generateUUIDv4())"]
        })

        sql_lines = self.generate_sql(df)

        assert len(sql_lines) == 3
        assert 'DEFAULT toDateTime(now())' in sql_lines[0]
        assert 'DEFAULT toUInt32(rand())' in sql_lines[1]
        assert "DEFAULT concat('prefix_', generateUUIDv4())" in sql_lines[2]

    def test_special_characters_in_strings_with_default(self):
        """
        Test: String literals with special characters
        Expected: Should preserve special characters in defaults
        """
        df = pd.DataFrame({
            'column': ['message', 'tag', 'code'],
            'type': ['String', 'String', 'String'],
            'default': ["'hello world!'", "'tag-123'", "'CODE_ABC_123'"]
        })

        sql_lines = self.generate_sql(df)

        assert len(sql_lines) == 3
        assert "DEFAULT 'hello world!'" in sql_lines[0]
        assert "DEFAULT 'tag-123'" in sql_lines[1]
        assert "DEFAULT 'CODE_ABC_123'" in sql_lines[2]

    def test_all_empty_defaults(self):
        """
        Test: All columns have empty defaults
        Expected: All should generate clean DDL without DEFAULT
        """
        df = pd.DataFrame({
            'column': ['col1', 'col2', 'col3', 'col4'],
            'type': ['String', 'String', 'String', 'String'],
            'default': ['', '   ', '\t', None]
        })

        sql_lines = self.generate_sql(df)

        assert len(sql_lines) == 4
        for line in sql_lines:
            assert 'DEFAULT' not in line
        assert sql_lines == [
            'col1 String',
            'col2 String',
            'col3 String',
            'col4 String'
        ]
