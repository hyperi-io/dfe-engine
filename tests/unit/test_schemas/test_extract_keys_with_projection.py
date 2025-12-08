"""
Unit tests for extracting PRIMARY KEY and ORDER BY from DDL with PROJECTION.
Tests the fix for the bug where ORDER BY inside PROJECTION was being extracted
instead of the table-level ORDER BY.

Related issue: Schema plan showing empty ORDER BY when PROJECTION exists.

This test covers:
1. ORDER BY with parentheses: ORDER BY (col1, col2, col3)
2. ORDER BY without parentheses: ORDER BY col1
3. Empty ORDER BY: ORDER BY ()
4. ORDER BY with PROJECTION (the main bug)
5. Multiple PROJECTION blocks
"""

import pytest
from pathlib import Path
import sys

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "src"))

from dfe_engine.schema.schema_util import SchemaUtils


class TestExtractKeysWithProjection:
    """Test extraction of PRIMARY KEY and ORDER BY when PROJECTION is present."""

    def test_order_by_with_parentheses_no_projection(self):
        """Test ORDER BY with parentheses - baseline without PROJECTION."""
        ddl = '''CREATE TABLE test_org.simple_table
(
    `timestamp_load` DateTime64(3, 'UTC'),
    `col1` String,
    `col2` String
)
ENGINE = MergeTree()
PRIMARY KEY (timestamp_load, col1, col2)
ORDER BY (timestamp_load, col1, col2)'''

        pk, ob, *_ = SchemaUtils.extract_keys_and_indexes_from_ddl(ddl)
        
        assert pk == "timestamp_load, col1, col2", f"Expected 'timestamp_load, col1, col2', got '{pk}'"
        assert ob == "timestamp_load, col1, col2", f"Expected 'timestamp_load, col1, col2', got '{ob}'"

    def test_order_by_without_parentheses_no_projection(self):
        """Test ORDER BY without parentheses (single column) - baseline without PROJECTION."""
        ddl = '''CREATE TABLE test_org.simple_table
(
    `timestamp_load` DateTime64(3, 'UTC'),
    `data` String
)
ENGINE = MergeTree()
PRIMARY KEY timestamp_load
ORDER BY timestamp_load'''

        pk, ob, *_ = SchemaUtils.extract_keys_and_indexes_from_ddl(ddl)
        
        assert pk == "timestamp_load", f"Expected 'timestamp_load', got '{pk}'"
        assert ob == "timestamp_load", f"Expected 'timestamp_load', got '{ob}'"

    def test_order_by_without_parentheses_with_ttl(self):
        """Test ORDER BY without parentheses followed by TTL."""
        ddl = '''CREATE TABLE test_org.table_with_ttl
(
    `timestamp` DateTime64(3, 'UTC'),
    `timestamp_load` DateTime64(3, 'UTC')
)
ENGINE = MergeTree()
PRIMARY KEY timestamp_load
ORDER BY timestamp_load
TTL toDateTime(timestamp) + toIntervalDay(90)'''

        pk, ob, *_ = SchemaUtils.extract_keys_and_indexes_from_ddl(ddl)
        
        assert pk == "timestamp_load", f"Expected 'timestamp_load', got '{pk}'"
        assert ob == "timestamp_load", f"Expected 'timestamp_load', got '{ob}'"

    def test_empty_order_by_with_parentheses(self):
        """Test empty ORDER BY: ORDER BY ()"""
        ddl = '''CREATE TABLE test_org.empty_order
(
    `event_id` Int32,
    `timestamp` DateTime
)
ENGINE = MergeTree()
PRIMARY KEY (event_id, timestamp)
ORDER BY ()'''

        pk, ob, *_ = SchemaUtils.extract_keys_and_indexes_from_ddl(ddl)
        
        assert pk == "event_id, timestamp", f"Expected 'event_id, timestamp', got '{pk}'"
        # Empty ORDER BY should return None or empty string
        assert ob is None or ob == "", f"Empty ORDER BY should be None or '', got '{ob}'"
        """Test ORDER BY extraction with PROJECTION - parenthesized format."""
        ddl = '''CREATE TABLE test_org.logs_alerts
(
    `timestamp` DateTime64(3, 'UTC') CODEC(DoubleDelta, LZ4),
    `timestamp_load` DateTime64(3, 'UTC') CODEC(DoubleDelta, LZ4),
    `detection_time` DateTime64(3, 'UTC') CODEC(DoubleDelta, LZ4),
    `source_table` LowCardinality(String) CODEC(LZ4),
    INDEX idx_timestamp timestamp TYPE set(0) GRANULARITY 4,
    PROJECTION timestamp_optimized
    (
        SELECT *
        ORDER BY timestamp
    )
)
ENGINE = SharedMergeTree('/clickhouse/tables/{uuid}/{shard}', '{replica}')
PARTITION BY toYYYYMMDD(timestamp_load)
PRIMARY KEY (timestamp_load, source_table, detection_time)
ORDER BY (timestamp_load, source_table, detection_time)
TTL toDateTime(timestamp) + toIntervalDay(180)
SETTINGS index_granularity = 2048'''

        pk, ob, *_ = SchemaUtils.extract_keys_and_indexes_from_ddl(ddl)
        
        # Should extract table-level ORDER BY, not the one inside PROJECTION
        assert pk == "timestamp_load, source_table, detection_time", \
            f"PRIMARY KEY should be 'timestamp_load, source_table, detection_time', got '{pk}'"
        assert ob == "timestamp_load, source_table, detection_time", \
            f"ORDER BY should be 'timestamp_load, source_table, detection_time', got '{ob}' (was PROJECTION ORDER BY extracted by mistake?)"

    def test_extract_order_by_with_projection_no_parentheses(self):
        """Test ORDER BY extraction with PROJECTION - non-parenthesized format."""
        ddl = '''CREATE TABLE test_org.logs_beats_filebeat
(
    `timestamp` DateTime64(3, 'UTC') CODEC(DoubleDelta, LZ4),
    `timestamp_load` DateTime64(3, 'UTC') CODEC(DoubleDelta, LZ4),
    INDEX idx_timestamp timestamp TYPE set(0) GRANULARITY 4,
    PROJECTION timestamp_optimized
    (
        SELECT *
        ORDER BY timestamp
    )
)
ENGINE = SharedMergeTree('/clickhouse/tables/{uuid}/{shard}', '{replica}')
PARTITION BY toYYYYMMDD(timestamp_load)
PRIMARY KEY timestamp_load
ORDER BY timestamp_load
TTL toDateTime(timestamp) + toIntervalDay(90)
SETTINGS index_granularity = 2048'''

        pk, ob, *_ = SchemaUtils.extract_keys_and_indexes_from_ddl(ddl)
        
        assert pk == "timestamp_load", f"PRIMARY KEY should be 'timestamp_load', got '{pk}'"
        assert ob == "timestamp_load", f"ORDER BY should be 'timestamp_load', got '{ob}' (was PROJECTION ORDER BY extracted by mistake?)"

    def test_extract_order_by_without_projection(self):
        """Test ORDER BY extraction without PROJECTION (baseline test)."""
        ddl = '''CREATE TABLE test_org.simple_table
(
    `timestamp` DateTime64(3, 'UTC'),
    `timestamp_load` DateTime64(3, 'UTC'),
    `data` String
)
ENGINE = MergeTree()
PARTITION BY toYYYYMMDD(timestamp_load)
PRIMARY KEY (timestamp_load, timestamp)
ORDER BY (timestamp_load, timestamp)
TTL toDateTime(timestamp) + toIntervalDay(30)'''

        pk, ob, *_ = SchemaUtils.extract_keys_and_indexes_from_ddl(ddl)
        
        assert pk == "timestamp_load, timestamp", f"PRIMARY KEY should be 'timestamp_load, timestamp', got '{pk}'"
        assert ob == "timestamp_load, timestamp", f"ORDER BY should be 'timestamp_load, timestamp', got '{ob}'"

    def test_extract_empty_order_by_with_fallback(self):
        """Test empty ORDER BY falls back to PRIMARY KEY."""
        ddl = '''CREATE TABLE test_org.empty_order_by
(
    `event_id` Int32,
    `timestamp` DateTime
)
ENGINE = MergeTree()
PRIMARY KEY (event_id, timestamp)
ORDER BY ()
TTL toDateTime(timestamp) + toIntervalDay(30)'''

        pk, ob, *_ = SchemaUtils.extract_keys_and_indexes_from_ddl(ddl)
        
        assert pk == "event_id, timestamp", f"PRIMARY KEY should be 'event_id, timestamp', got '{pk}'"
        # Empty ORDER BY should be normalized to None
        assert ob is None or ob == "", f"Empty ORDER BY should be None or empty string, got '{ob}'"

    def test_extract_complex_ddl_with_multiple_projections(self):
        """Test extraction from complex DDL with multiple projections."""
        ddl = '''CREATE TABLE test_org.complex_table
(
    `timestamp` DateTime64(3, 'UTC') CODEC(DoubleDelta, LZ4),
    `timestamp_load` DateTime64(3, 'UTC') CODEC(DoubleDelta, LZ4),
    `user_id` String CODEC(LZ4),
    `event_type` LowCardinality(String) CODEC(LZ4),
    INDEX idx_user user_id TYPE bloom_filter GRANULARITY 4,
    INDEX idx_event event_type TYPE set(0) GRANULARITY 4,
    PROJECTION by_timestamp
    (
        SELECT *
        ORDER BY timestamp
    ),
    PROJECTION by_user
    (
        SELECT *
        ORDER BY user_id, timestamp
    )
)
ENGINE = SharedMergeTree('/clickhouse/tables/{uuid}/{shard}', '{replica}')
PARTITION BY toYYYYMMDD(timestamp_load)
PRIMARY KEY (timestamp_load, event_type, user_id)
ORDER BY (timestamp_load, event_type, user_id)
SAMPLE BY cityHash64(user_id)
TTL toDateTime(timestamp) + toIntervalDay(90)
SETTINGS index_granularity = 8192'''

        pk, ob, indexes, *_ = SchemaUtils.extract_keys_and_indexes_from_ddl(ddl)
        
        assert pk == "timestamp_load, event_type, user_id", \
            f"PRIMARY KEY should be 'timestamp_load, event_type, user_id', got '{pk}'"
        assert ob == "timestamp_load, event_type, user_id", \
            f"ORDER BY should be 'timestamp_load, event_type, user_id', got '{ob}'"
        assert len(indexes) == 2, f"Should extract 2 indexes, got {len(indexes)}"
        # Note: TTL extraction with WHERE clause has limitations - not testing it here

    def test_real_world_logs_alerts_schema(self):
        """Test with the exact real-world logs_alerts schema that was failing."""
        ddl = '''CREATE TABLE test_org_08102025.logs_alerts
(
    `timestamp` DateTime64(3, 'UTC') CODEC(DoubleDelta, LZ4),
    `timestamp_collector` DateTime64(3, 'UTC') CODEC(DoubleDelta, LZ4),
    `timestamp_load` DateTime64(3, 'UTC') CODEC(DoubleDelta, LZ4),
    `timestamp_received` DateTime64(3, 'UTC') CODEC(DoubleDelta, LZ4),
    `timestamp_finalise` DateTime64(3, 'UTC') CODEC(DoubleDelta, LZ4),
    `timestamp_epochms` Nullable(Int64) CODEC(ZSTD(1)),
    `timestamp_collector_epochms` Nullable(Int64) CODEC(ZSTD(1)),
    `timestamp_load_epochms` Nullable(Int64) CODEC(ZSTD(1)),
    `timestamp_received_epochms` Nullable(Int64) CODEC(ZSTD(1)),
    `timestamp_finalise_epochms` Nullable(Int64) CODEC(ZSTD(1)),
    `event_hash` String CODEC(LZ4),
    `logoriginal` String CODEC(ZSTD(1)),
    `logjson` JSON,
    `org_id` LowCardinality(String) CODEC(LZ4),
    `tags_collector_host` LowCardinality(String) CODEC(LZ4),
    `tags_collector_hostname` LowCardinality(String) CODEC(LZ4),
    `tags_collector_source` LowCardinality(String) CODEC(LZ4),
    `tags_collector_timestamp` Nullable(DateTime64(3, 'UTC')) CODEC(DoubleDelta, ZSTD(1)),
    `tags_collector_timezone` String CODEC(ZSTD(1)),
    `tags_event_category` LowCardinality(String) CODEC(LZ4),
    `tags_event_org_id` LowCardinality(String) CODEC(LZ4),
    `tags_event_site_id` LowCardinality(String) CODEC(LZ4),
    `tags_event_type` LowCardinality(String) CODEC(LZ4),
    `tags_event_error` LowCardinality(String) CODEC(LZ4),
    `detection_time` DateTime64(3, 'UTC') CODEC(DoubleDelta, LZ4),
    `event` String CODEC(ZSTD(1)),
    `event_id` String CODEC(LZ4),
    `remediation_steps` String CODEC(LZ4),
    `rule_id` Nullable(UUID) CODEC(ZSTD(1)),
    `source_table` LowCardinality(String) CODEC(LZ4),
    `tactics` String CODEC(LZ4),
    `techniques` String CODEC(LZ4),
    INDEX idx_timestamp timestamp TYPE set(0) GRANULARITY 4,
    PROJECTION timestamp_optimized
    (
        SELECT *
        ORDER BY timestamp
    )
)
ENGINE = SharedMergeTree('/clickhouse/tables/{uuid}/{shard}', '{replica}')
PARTITION BY toYYYYMMDD(timestamp_load)
PRIMARY KEY (timestamp_load, source_table, detection_time)
ORDER BY (timestamp_load, source_table, detection_time)
TTL toDateTime(timestamp) + toIntervalDay(180) WHERE timestamp IS NOT NULL, toDateTime(timestamp_load) + toIntervalDay(180) WHERE timestamp_load IS NOT NULL
SETTINGS index_granularity = 2048, ttl_only_drop_parts = 1'''

        pk, ob, indexes, ttl, sample_by, partition_by, projection, settings = SchemaUtils.extract_keys_and_indexes_from_ddl(ddl)
        
        # This was the bug: ORDER BY was being extracted as "timestamp" (from PROJECTION) 
        # instead of "timestamp_load, source_table, detection_time" (from table level)
        assert pk == "timestamp_load, source_table, detection_time", \
            f"PRIMARY KEY extraction failed: expected 'timestamp_load, source_table, detection_time', got '{pk}'"
        assert ob == "timestamp_load, source_table, detection_time", \
            f"ORDER BY extraction failed: expected 'timestamp_load, source_table, detection_time', got '{ob}' - BUG: PROJECTION ORDER BY was extracted instead of table ORDER BY"
        
        # Verify other extractions
        assert partition_by == "toYYYYMMDD(timestamp_load)", f"PARTITION BY should be 'toYYYYMMDD(timestamp_load)', got '{partition_by}'"
        assert len(indexes) == 1, f"Should extract 1 index, got {len(indexes)}"
        assert indexes[0][0] == "idx_timestamp", f"Index name should be 'idx_timestamp', got '{indexes[0][0]}'"
        assert projection is not None, "Should extract PROJECTION"
        assert projection[0] == "timestamp_optimized", f"Projection name should be 'timestamp_optimized', got '{projection[0]}'"
        assert settings == "index_granularity = 2048, ttl_only_drop_parts = 1", f"Settings should be 'index_granularity = 2048, ttl_only_drop_parts = 1', got '{settings}'"
