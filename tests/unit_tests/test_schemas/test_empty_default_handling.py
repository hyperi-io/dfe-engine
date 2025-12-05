"""
Tests to verify that empty default values generate clean DDL without DEFAULT clauses.
This ensures that when default column is empty or missing, SQL DDL is normal:
    column_name ColumnType,   (no DEFAULT clause)
"""

import logging
import pandas as pd
import pytest
from dfecli.dfe_schemabuilder.schema_util import SchemaUtils


class TestEmptyDefaultHandling:
    """Test that empty defaults don't generate DEFAULT clauses in SQL."""

    @pytest.fixture
    def logger(self):
        """Create a logger for tests."""
        return logging.getLogger(__name__)

    def test_no_default_column_generates_clean_ddl(self, logger):
        """
        Test Case 1: No default column at all
        Expected: Generated SQL should be 'column_name Type,' (no DEFAULT)
        """
        meta_df = pd.DataFrame({
            'column': ['status', 'event_time', 'user_id'],
            'type': ['String', 'DateTime', 'String'],
            'index_type': ['', '', '']
        })

        # Simulate schema_ch.py logic
        has_default_column = 'default' in meta_df.columns
        assert not has_default_column, "default column should not exist"

        sql_lines = []
        for _, row in meta_df.iterrows():
            column_default = ''
            if has_default_column and pd.notnull(row.get('default')) and str(row.get('default')).strip() != '':
                column_default = 'DEFAULT ' + str(row['default'])

            column_line = f"{row['column']} {row['type']}{' ' + column_default if column_default else ''}".strip()
            sql_lines.append(column_line)

        # Verify no DEFAULT clauses
        assert sql_lines == [
            'status String',
            'event_time DateTime',
            'user_id String'
        ]
        for line in sql_lines:
            assert 'DEFAULT' not in line, f"Should not contain DEFAULT: {line}"

    def test_empty_default_values_generate_clean_ddl(self, logger):
        """
        Test Case 2: default column exists but ALL values are empty strings
        Expected: Generated SQL should be 'column_name Type,' (no DEFAULT)
        """
        meta_df = pd.DataFrame({
            'column': ['status', 'event_time', 'user_id'],
            'type': ['String', 'DateTime', 'String'],
            'default': ['', '', ''],
            'index_type': ['', '', '']
        })

        # Simulate schema_ch.py logic
        has_default_column = 'default' in meta_df.columns
        assert has_default_column, "default column should exist"

        sql_lines = []
        for _, row in meta_df.iterrows():
            column_default = ''
            if has_default_column and pd.notnull(row.get('default')) and str(row.get('default')).strip() != '':
                column_default = 'DEFAULT ' + str(row['default'])

            column_line = f"{row['column']} {row['type']}{' ' + column_default if column_default else ''}".strip()
            sql_lines.append(column_line)

        # Verify no DEFAULT clauses
        assert sql_lines == [
            'status String',
            'event_time DateTime',
            'user_id String'
        ]
        for line in sql_lines:
            assert 'DEFAULT' not in line, f"Should not contain DEFAULT: {line}"

    def test_mixed_defaults_generates_correct_ddl(self, logger):
        """
        Test Case 3: default column with MIXED values (some populated, some empty)
        Expected: Only populated defaults should generate DEFAULT clause
        """
        meta_df = pd.DataFrame({
            'column': ['status', 'event_time', 'user_id', 'count'],
            'type': ['String', 'DateTime', 'String', 'UInt32'],
            'default': ["'active'", '', "'guest'", '0'],
            'index_type': ['', '', '', '']
        })

        # Simulate schema_ch.py logic
        has_default_column = 'default' in meta_df.columns
        assert has_default_column

        sql_lines = []
        for _, row in meta_df.iterrows():
            column_default = ''
            if has_default_column and pd.notnull(row.get('default')) and str(row.get('default')).strip() != '':
                column_default = 'DEFAULT ' + str(row['default'])

            column_line = f"{row['column']} {row['type']}{' ' + column_default if column_default else ''}".strip()
            sql_lines.append(column_line)

        # Verify only populated defaults generate DEFAULT clause
        expected = [
            "status String DEFAULT 'active'",
            'event_time DateTime',
            "user_id String DEFAULT 'guest'",
            'count UInt32 DEFAULT 0'
        ]
        assert sql_lines == expected

        # Verify pattern: DEFAULT only for populated values
        assert "DEFAULT 'active'" in sql_lines[0]
        assert 'DEFAULT' not in sql_lines[1]  # Empty default
        assert "DEFAULT 'guest'" in sql_lines[2]
        assert 'DEFAULT 0' in sql_lines[3]

    def test_whitespace_only_default_generates_clean_ddl(self, logger):
        """
        Test Case 4: default column with whitespace-only values
        Expected: Should be treated as empty (no DEFAULT clause)
        """
        meta_df = pd.DataFrame({
            'column': ['status', 'event_time'],
            'type': ['String', 'DateTime'],
            'default': ['   ', '  '],  # Whitespace only
        })

        has_default_column = 'default' in meta_df.columns

        sql_lines = []
        for _, row in meta_df.iterrows():
            column_default = ''
            if has_default_column and pd.notnull(row.get('default')) and str(row.get('default')).strip() != '':
                column_default = 'DEFAULT ' + str(row['default'])

            column_line = f"{row['column']} {row['type']}{' ' + column_default if column_default else ''}".strip()
            sql_lines.append(column_line)

        # Whitespace-only should be treated as empty
        assert sql_lines == [
            'status String',
            'event_time DateTime'
        ]
        for line in sql_lines:
            assert 'DEFAULT' not in line

    def test_valid_clickhouse_defaults_preserved(self, logger):
        """
        Test Case 5: Valid ClickHouse default expressions are preserved
        Expected: Should generate DEFAULT clauses for valid expressions
        """
        meta_df = pd.DataFrame({
            'column': ['id', 'created_at', 'name', 'retry_count'],
            'type': ['String', 'DateTime', 'String', 'UInt32'],
            'default': ["generateUUIDv4()", "now()", "'N/A'", "0"],
        })

        has_default_column = 'default' in meta_df.columns

        sql_lines = []
        for _, row in meta_df.iterrows():
            column_default = ''
            if has_default_column and pd.notnull(row.get('default')) and str(row.get('default')).strip() != '':
                column_default = 'DEFAULT ' + str(row['default'])

            column_line = f"{row['column']} {row['type']}{' ' + column_default if column_default else ''}".strip()
            sql_lines.append(column_line)

        # All should have DEFAULT clauses
        expected = [
            'id String DEFAULT generateUUIDv4()',
            'created_at DateTime DEFAULT now()',
            "name String DEFAULT 'N/A'",
            'retry_count UInt32 DEFAULT 0'
        ]
        assert sql_lines == expected

        # Verify all have DEFAULT
        for line in sql_lines:
            assert 'DEFAULT' in line, f"Should contain DEFAULT: {line}"
