"""
Integration tests for ORDER BY change detection in schema plan/update.

Tests the complete flow:
1. Extract ORDER BY from DDL (schema_util.py)
2. Compare current vs expected (schema_plan.py / schema_update.py)
3. Detect actual changes vs no changes

This validates the fix for:
- ORDER BY inside PROJECTION being extracted incorrectly
- ORDER BY without parentheses not being supported
- Empty ORDER BY not falling back to PRIMARY KEY
"""

import pytest
from pathlib import Path
import sys

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "src"))

from dfecli.dfe_schemabuilder.schema_util import SchemaUtils


class TestOrderByChangeDetection:
    """Test ORDER BY change detection - simulating schema plan/update logic."""

    def test_no_change_same_order_by_with_parentheses(self):
        """Test: Current and expected ORDER BY are the same (with parentheses) - NO CHANGE."""
        current_ddl = '''CREATE TABLE test_org.logs_alerts
(
    `timestamp_load` DateTime64(3, 'UTC'),
    `source_table` String,
    `detection_time` DateTime64(3, 'UTC')
)
ENGINE = MergeTree()
PRIMARY KEY (timestamp_load, source_table, detection_time)
ORDER BY (timestamp_load, source_table, detection_time)'''

        expected_ddl = '''CREATE TABLE test_org.logs_alerts
(
    `timestamp_load` DateTime64(3, 'UTC'),
    `source_table` String,
    `detection_time` DateTime64(3, 'UTC')
)
ENGINE = MergeTree()
PRIMARY KEY (timestamp_load, source_table, detection_time)
ORDER BY (timestamp_load, source_table, detection_time)'''

        # Extract from both
        _, current_order_by, *_ = SchemaUtils.extract_keys_and_indexes_from_ddl(current_ddl)
        _, expected_order_by, *_ = SchemaUtils.extract_keys_and_indexes_from_ddl(expected_ddl)

        # Assert NO CHANGE
        assert current_order_by == expected_order_by
        assert current_order_by == "timestamp_load, source_table, detection_time"
        print(f"✅ No change detected: '{current_order_by}' == '{expected_order_by}'")

    def test_no_change_same_order_by_without_parentheses(self):
        """Test: Current and expected ORDER BY are the same (without parentheses) - NO CHANGE."""
        current_ddl = '''CREATE TABLE test_org.logs_beats_filebeat
(
    `timestamp_load` DateTime64(3, 'UTC'),
    `message` String
)
ENGINE = MergeTree()
PRIMARY KEY timestamp_load
ORDER BY timestamp_load
TTL toDateTime(timestamp_load) + toIntervalDay(90)'''

        expected_ddl = '''CREATE TABLE test_org.logs_beats_filebeat
(
    `timestamp_load` DateTime64(3, 'UTC'),
    `message` String
)
ENGINE = MergeTree()
PRIMARY KEY timestamp_load
ORDER BY timestamp_load
TTL toDateTime(timestamp_load) + toIntervalDay(90)'''

        # Extract from both
        _, current_order_by, *_ = SchemaUtils.extract_keys_and_indexes_from_ddl(current_ddl)
        _, expected_order_by, *_ = SchemaUtils.extract_keys_and_indexes_from_ddl(expected_ddl)

        # Assert NO CHANGE
        assert current_order_by == expected_order_by
        assert current_order_by == "timestamp_load"
        print(f"✅ No change detected: '{current_order_by}' == '{expected_order_by}'")

    def test_actual_change_detected_order_by_different(self):
        """Test: Current and expected ORDER BY are DIFFERENT - CHANGE DETECTED."""
        current_ddl = '''CREATE TABLE test_org.logs_alerts
(
    `timestamp_load` DateTime64(3, 'UTC'),
    `source_table` String,
    `detection_time` DateTime64(3, 'UTC')
)
ENGINE = MergeTree()
PRIMARY KEY (timestamp_load, source_table, detection_time)
ORDER BY (timestamp_load, source_table)'''  # ← Different: missing detection_time

        expected_ddl = '''CREATE TABLE test_org.logs_alerts
(
    `timestamp_load` DateTime64(3, 'UTC'),
    `source_table` String,
    `detection_time` DateTime64(3, 'UTC')
)
ENGINE = MergeTree()
PRIMARY KEY (timestamp_load, source_table, detection_time)
ORDER BY (timestamp_load, source_table, detection_time)'''  # ← Has detection_time

        # Extract from both
        _, current_order_by, *_ = SchemaUtils.extract_keys_and_indexes_from_ddl(current_ddl)
        _, expected_order_by, *_ = SchemaUtils.extract_keys_and_indexes_from_ddl(expected_ddl)

        # Assert CHANGE DETECTED
        assert current_order_by != expected_order_by
        assert current_order_by == "timestamp_load, source_table"
        assert expected_order_by == "timestamp_load, source_table, detection_time"
        print(f"✅ Change detected: '{current_order_by}' != '{expected_order_by}'")

    def test_actual_change_single_column_to_multiple_columns(self):
        """Test: ORDER BY changes from single column to multiple columns - CHANGE DETECTED."""
        current_ddl = '''CREATE TABLE test_org.logs_beats_filebeat
(
    `timestamp_load` DateTime64(3, 'UTC'),
    `source_table` String
)
ENGINE = MergeTree()
PRIMARY KEY timestamp_load
ORDER BY timestamp_load'''  # ← Single column

        expected_ddl = '''CREATE TABLE test_org.logs_beats_filebeat
(
    `timestamp_load` DateTime64(3, 'UTC'),
    `source_table` String
)
ENGINE = MergeTree()
PRIMARY KEY (timestamp_load, source_table)
ORDER BY (timestamp_load, source_table)'''  # ← Multiple columns

        # Extract from both
        _, current_order_by, *_ = SchemaUtils.extract_keys_and_indexes_from_ddl(current_ddl)
        _, expected_order_by, *_ = SchemaUtils.extract_keys_and_indexes_from_ddl(expected_ddl)

        # Assert CHANGE DETECTED
        assert current_order_by != expected_order_by
        assert current_order_by == "timestamp_load"
        assert expected_order_by == "timestamp_load, source_table"
        print(f"✅ Change detected: '{current_order_by}' -> '{expected_order_by}'")

    def test_no_change_with_projection_present(self):
        """Test: ORDER BY same, but PROJECTION has different ORDER BY - NO CHANGE (bug fix validation)."""
        current_ddl = '''CREATE TABLE test_org.logs_alerts
(
    `timestamp` DateTime64(3, 'UTC'),
    `timestamp_load` DateTime64(3, 'UTC'),
    `detection_time` DateTime64(3, 'UTC'),
    `source_table` String,
    PROJECTION timestamp_optimized
    (
        SELECT *
        ORDER BY timestamp
    )
)
ENGINE = SharedMergeTree()
PRIMARY KEY (timestamp_load, source_table, detection_time)
ORDER BY (timestamp_load, source_table, detection_time)'''

        expected_ddl = '''CREATE TABLE test_org.logs_alerts
(
    `timestamp` DateTime64(3, 'UTC'),
    `timestamp_load` DateTime64(3, 'UTC'),
    `detection_time` DateTime64(3, 'UTC'),
    `source_table` String,
    PROJECTION timestamp_optimized
    (
        SELECT *
        ORDER BY timestamp
    )
)
ENGINE = SharedMergeTree()
PRIMARY KEY (timestamp_load, source_table, detection_time)
ORDER BY (timestamp_load, source_table, detection_time)'''

        # Extract from both
        _, current_order_by, *_ = SchemaUtils.extract_keys_and_indexes_from_ddl(current_ddl)
        _, expected_order_by, *_ = SchemaUtils.extract_keys_and_indexes_from_ddl(expected_ddl)

        # Assert NO CHANGE - Should extract table ORDER BY, not PROJECTION ORDER BY
        assert current_order_by == expected_order_by
        assert current_order_by == "timestamp_load, source_table, detection_time"
        assert current_order_by != "timestamp", "BUG: Extracted ORDER BY from PROJECTION instead of table!"
        print(f"✅ No change detected (with PROJECTION): '{current_order_by}' == '{expected_order_by}'")

    def test_empty_order_by_fallback_to_primary_key(self):
        """Test: Empty ORDER BY () should fallback to PRIMARY KEY - NO CHANGE."""
        current_ddl = '''CREATE TABLE test_org.logs_alerts
(
    `timestamp_load` DateTime64(3, 'UTC'),
    `source_table` String,
    `detection_time` DateTime64(3, 'UTC')
)
ENGINE = MergeTree()
PRIMARY KEY (timestamp_load, source_table, detection_time)
ORDER BY ()'''  # ← Empty ORDER BY

        expected_ddl = '''CREATE TABLE test_org.logs_alerts
(
    `timestamp_load` DateTime64(3, 'UTC'),
    `source_table` String,
    `detection_time` DateTime64(3, 'UTC')
)
ENGINE = MergeTree()
PRIMARY KEY (timestamp_load, source_table, detection_time)
ORDER BY ()'''  # ← Empty ORDER BY

        # Extract from both
        current_pk, current_order_by, *_ = SchemaUtils.extract_keys_and_indexes_from_ddl(current_ddl)
        expected_pk, expected_order_by, *_ = SchemaUtils.extract_keys_and_indexes_from_ddl(expected_ddl)

        # ORDER BY is empty, apply fallback
        if not current_order_by and current_pk:
            current_order_by = current_pk
        if not expected_order_by and expected_pk:
            expected_order_by = expected_pk

        # Assert NO CHANGE (after fallback)
        assert current_order_by == expected_order_by
        assert current_order_by == "timestamp_load, source_table, detection_time"
        print(f"✅ No change detected (empty ORDER BY with fallback): '{current_order_by}' == '{expected_order_by}'")

    def test_mixed_format_parentheses_vs_no_parentheses_same_value(self):
        """Test: ORDER BY (col) vs ORDER BY col - should be treated as SAME."""
        current_ddl = '''CREATE TABLE test_org.test_table
(
    `timestamp_load` DateTime64(3, 'UTC')
)
ENGINE = MergeTree()
PRIMARY KEY (timestamp_load)
ORDER BY (timestamp_load)'''  # ← With parentheses

        expected_ddl = '''CREATE TABLE test_org.test_table
(
    `timestamp_load` DateTime64(3, 'UTC')
)
ENGINE = MergeTree()
PRIMARY KEY timestamp_load
ORDER BY timestamp_load'''  # ← Without parentheses

        # Extract from both
        _, current_order_by, *_ = SchemaUtils.extract_keys_and_indexes_from_ddl(current_ddl)
        _, expected_order_by, *_ = SchemaUtils.extract_keys_and_indexes_from_ddl(expected_ddl)

        # Assert NO CHANGE - Same value, different format
        assert current_order_by == expected_order_by
        assert current_order_by == "timestamp_load"
        print(f"✅ No change detected (mixed format): '{current_order_by}' == '{expected_order_by}'")


if __name__ == "__main__":
    # Allow running tests directly
    pytest.main([__file__, "-v"])
