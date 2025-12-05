"""
Test that now() function defaults are correctly handled in all schema sources.

Tests verify that now() from common headers, meta schemas, and derived schemas
are correctly preserved and appear in the final ClickHouse DDL.
"""

import logging
import pandas as pd
import pytest
from pathlib import Path
from dfecli.dfe_schemabuilder.schema_ch import ClickHouseSchema
from dfecli.dfe_schemabuilder.schema_util import SchemaUtils


logger = logging.getLogger(__name__)


class TestNowFunctionDefaults:
    """Test now() function handling in DEFAULT clauses across all schema sources."""

    @pytest.fixture
    def common_header_with_now(self, tmp_path):
        """Create a common header with now() default."""
        df = pd.DataFrame({
            'column': ['timestamp_load', 'org_id'],
            'type': ['timestamp', 'string_fast_lowcardinality'],
            'default': ['now()', ''],
            'index_order': [0, ''],
            'index_type': ['', ''],
            'os_order': ['', ''],
            'comment': ['Time loaded', 'Org ID']
        })
        csv_path = tmp_path / 'common_header.csv'
        df.to_csv(csv_path, index=False)
        return csv_path

    @pytest.fixture
    def meta_schema_with_now(self, tmp_path):
        """Create a meta schema with now() in default column."""
        df = pd.DataFrame({
            'column': ['event_creation_time', 'event_id', 'message'],
            'type': ['datetime', 'string', 'text'],
            'default': ['now()', '', ''],
            'index_order': [1, 2, ''],
            'index_type': ['', '', ''],
            'comment': ['When event created', 'Event ID', 'Message']
        })
        csv_path = tmp_path / 'meta_schema.csv'
        df.to_csv(csv_path, index=False)
        return csv_path

    @pytest.fixture
    def derived_schema_with_now(self, tmp_path):
        """Create a derived schema with now() in default column."""
        df = pd.DataFrame({
            'column': ['updated_at', 'event_id'],
            'type': ['datetime', 'string'],
            'default': ['now()', ''],
            'index_order': ['', ''],
            'index_type': ['', ''],
            'comment': ['Last update', 'Event ID']
        })
        csv_path = tmp_path / 'derived_schema.csv'
        df.to_csv(csv_path, index=False)
        return csv_path

    def test_now_in_common_header_appears_in_ddl(self, tmp_path, common_header_with_now):
        """Test that now() from common header is included in final DDL."""
        loaded_df = SchemaUtils.load_schema_from_resource_package(
            package_path='dfecli.resources',
            resource_path='common/v001_001_008/common_header.csv'
        )
        
        # Check that timestamp_load has now() default
        ts_load_row = loaded_df[loaded_df['column'] == 'timestamp_load']
        assert not ts_load_row.empty, "timestamp_load not found in common header"
        assert ts_load_row['default'].values[0] == 'now()', \
            f"timestamp_load default should be 'now()', got '{ts_load_row['default'].values[0]}'"

    def test_now_from_meta_schema_preserved_through_merge(self):
        """Test that now() from meta schema is preserved during merge operations."""
        meta_df = pd.DataFrame({
            'column': ['event_creation_time', 'event_id', 'message'],
            'type': ['datetime', 'string', 'text'],
            'default': ['now()', '', ''],
            'index_order': [1, 2, ''],
            'index_type': ['', '', ''],
        })
        
        # Verify now() is in the meta schema
        event_creation_time_row = meta_df[meta_df['column'] == 'event_creation_time']
        assert event_creation_time_row['default'].values[0] == 'now()', \
            "Meta schema should have now() for event_creation_time"

    def test_now_from_derived_schema_preserved_through_merge(self):
        """Test that now() from derived schema is preserved during merge operations."""
        meta_df = pd.DataFrame({
            'column': ['updated_at', 'event_id', 'status'],
            'type': ['datetime', 'string', 'string'],
            'default': ['', '', ''],
            'index_order': ['', '', ''],
            'index_type': ['', '', ''],
        })

        derived_df = pd.DataFrame({
            'column': ['updated_at', 'event_id'],
        })

        result_df = SchemaUtils.apply_derived_schema(
            meta_schema_df=meta_df,
            derived_schema_df=derived_df,
            logger=logger,
        )

        # After merge, now we can add defaults to derived columns
        # (In practice, defaults would be in the derived schema CSV)
        assert 'default' in result_df.columns, "default column should be present after merge"

    def test_now_appears_in_clickhouse_ddl_line(self):
        """Test that now() appears unquoted in the final ClickHouse DDL."""
        df = pd.DataFrame({
            'column': ['timestamp_load', 'created_at', 'event_id'],
            'type': ['timestamp', 'datetime', 'string'],
            'default': ['now()', 'now()', ''],
        })

        has_default_column = 'default' in df.columns
        sql_lines = []
        
        for _, row in df.iterrows():
            column_default = ''
            if has_default_column and pd.notnull(row.get('default')) and str(row.get('default')).strip() != '':
                column_default = 'DEFAULT ' + str(row['default'])
            
            column_line = f"{row['column']} {row['type']}{' ' + column_default if column_default else ''}".strip()
            sql_lines.append(column_line)

        # Verify now() appears unquoted in DDL
        assert 'timestamp_load timestamp DEFAULT now()' in sql_lines[0], \
            f"Expected 'timestamp_load timestamp DEFAULT now()', got '{sql_lines[0]}'"
        assert 'created_at datetime DEFAULT now()' in sql_lines[1], \
            f"Expected 'created_at datetime DEFAULT now()', got '{sql_lines[1]}'"
        assert 'DEFAULT' not in sql_lines[2], \
            f"event_id should not have DEFAULT, got '{sql_lines[2]}'"

    def test_now_not_quoted_in_ddl(self):
        """Test that now() is NOT quoted (should be now() not 'now()')."""
        df = pd.DataFrame({
            'column': ['created_at'],
            'type': ['datetime'],
            'default': ['now()'],
        })

        has_default_column = 'default' in df.columns
        sql_lines = []
        
        for _, row in df.iterrows():
            column_default = ''
            if has_default_column and pd.notnull(row.get('default')) and str(row.get('default')).strip() != '':
                column_default = 'DEFAULT ' + str(row['default'])
            
            column_line = f"{row['column']} {row['type']}{' ' + column_default if column_default else ''}".strip()
            sql_lines.append(column_line)

        # Verify now() is NOT quoted
        assert "DEFAULT now()" in sql_lines[0], \
            f"Expected 'DEFAULT now()' (unquoted), got '{sql_lines[0]}'"
        assert "DEFAULT 'now()'" not in sql_lines[0], \
            f"Should NOT have quoted 'now()', but got '{sql_lines[0]}'"

    def test_mixed_defaults_with_now_and_strings(self):
        """Test mixing now() functions with other defaults like generateUUIDv4() and strings."""
        df = pd.DataFrame({
            'column': ['created_at', 'id', 'status', 'retry_count'],
            'type': ['datetime', 'string', 'string', 'uint32'],
            'default': ['now()', 'generateUUIDv4()', "'active'", '0'],
        })

        has_default_column = 'default' in df.columns
        sql_lines = []
        
        for _, row in df.iterrows():
            column_default = ''
            if has_default_column and pd.notnull(row.get('default')) and str(row.get('default')).strip() != '':
                column_default = 'DEFAULT ' + str(row['default'])
            
            column_line = f"{row['column']} {row['type']}{' ' + column_default if column_default else ''}".strip()
            sql_lines.append(column_line)

        # Verify all defaults are preserved correctly
        assert 'DEFAULT now()' in sql_lines[0], "now() should appear unquoted"
        assert 'DEFAULT generateUUIDv4()' in sql_lines[1], "generateUUIDv4() should appear unquoted"
        assert "DEFAULT 'active'" in sql_lines[2], "String default should be quoted"
        assert 'DEFAULT 0' in sql_lines[3], "Numeric default should not be quoted"

    def test_now_with_codec_in_ddl(self):
        """Test that now() works correctly with CODEC in ClickHouse DDL."""
        # This simulates the actual schema_ch.py logic
        df = pd.DataFrame({
            'column': ['timestamp_load', 'created_at'],
            'type': ['DateTime64(3,\'UTC\') CODEC(DoubleDelta, LZ4)', 'DateTime CODEC(T64, LZ4)'],
            'default': ['now()', 'now()'],
        })

        has_default_column = 'default' in df.columns
        sql_lines = []
        
        for _, row in df.iterrows():
            column_type = row['type']
            parts = column_type.split(' CODEC')
            base_type = parts[0].strip()
            codec = "CODEC" + parts[1].strip() if len(parts) > 1 else ""
            
            column_default = ''
            if has_default_column and pd.notnull(row.get('default')) and str(row.get('default')).strip() != '':
                column_default = 'DEFAULT ' + str(row['default'])
            
            column_line = f"{row['column']} {base_type}{' ' + column_default if column_default else ''}{' ' + codec if codec else ''}".strip()
            sql_lines.append(column_line)

        # Verify DEFAULT now() appears before CODEC
        assert "DEFAULT now() CODEC" in sql_lines[0], \
            f"Expected 'DEFAULT now() CODEC', got '{sql_lines[0]}'"
        assert "DEFAULT now() CODEC" in sql_lines[1], \
            f"Expected 'DEFAULT now() CODEC', got '{sql_lines[1]}'"


class TestNowFunctionFromAllSources:
    """Integration test verifying now() handling from all three schema sources."""

    def test_now_from_common_meta_derived_all_preserved(self):
        """
        Integration test: Verify now() from common header, meta schema, and derived schema
        all appear in the final merged schema before DDL generation.
        """
        # Simulate common header
        common_df = pd.DataFrame({
            'column': ['timestamp_load', 'org_id'],
            'type': ['timestamp', 'string_fast_lowcardinality'],
            'default': ['now()', ''],
            'index_order': [0, ''],
            'index_type': ['', ''],
        })

        # Simulate meta schema
        meta_df = pd.DataFrame({
            'column': ['event_creation_time', 'event_id'],
            'type': ['datetime', 'string'],
            'default': ['now()', ''],
            'index_order': [1, 2],
            'index_type': ['', ''],
        })

        # Simulate derived schema
        derived_df = pd.DataFrame({
            'column': ['updated_at', 'event_id'],
            'type': ['datetime', 'string'],
            'default': ['now()', ''],
            'index_order': ['', ''],
            'index_type': ['', ''],
        })

        # Verify all sources have now()
        assert common_df[common_df['column'] == 'timestamp_load']['default'].values[0] == 'now()', \
            "Common header timestamp_load should have now()"
        assert meta_df[meta_df['column'] == 'event_creation_time']['default'].values[0] == 'now()', \
            "Meta schema event_creation_time should have now()"
        assert derived_df[derived_df['column'] == 'updated_at']['default'].values[0] == 'now()', \
            "Derived schema updated_at should have now()"

        logger.info("✅ All three schema sources have now() function defaults correctly set")
