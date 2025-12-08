"""
Comprehensive unit tests for extracting PRIMARY KEY and ORDER BY from DDL.

Tests cover:
1. ORDER BY with parentheses: ORDER BY (col1, col2, col3)
2. ORDER BY without parentheses: ORDER BY col1
3. Empty ORDER BY: ORDER BY ()
4. ORDER BY with PROJECTION (bug fix: should extract table ORDER BY, not PROJECTION ORDER BY)
5. Multiple PROJECTION blocks
6. Complex real-world scenarios

Bug Context: 
Schema plan was showing empty ORDER BY because the parser was matching
ORDER BY inside PROJECTION instead of the table-level ORDER BY.
"""

import pytest
from pathlib import Path
import sys

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "src"))

from dfe_engine.schema.schema_util import SchemaUtils


class TestOrderByExtractionBasics:
    """Test basic ORDER BY extraction without PROJECTION."""

    def test_order_by_with_parentheses_multiple_columns(self):
        """Test ORDER BY with parentheses and multiple columns."""
        ddl = '''CREATE TABLE test_org.multi_col
(
    `timestamp_load` DateTime64(3, 'UTC'),
    `col1` String,
    `col2` String,
    `col3` Int64
)
ENGINE = MergeTree()
PRIMARY KEY (timestamp_load, col1, col2)
ORDER BY (timestamp_load, col1, col2)'''

        pk, ob, *_ = SchemaUtils.extract_keys_and_indexes_from_ddl(ddl)
        
        assert pk == "timestamp_load, col1, col2", f"Expected 'timestamp_load, col1, col2', got '{pk}'"
        assert ob == "timestamp_load, col1, col2", f"Expected 'timestamp_load, col1, col2', got '{ob}'"

    def test_order_by_with_parentheses_single_column(self):
        """Test ORDER BY with parentheses and single column."""
        ddl = '''CREATE TABLE test_org.single_col_paren
(
    `timestamp_load` DateTime64(3, 'UTC'),
    `data` String
)
ENGINE = MergeTree()
PRIMARY KEY (timestamp_load)
ORDER BY (timestamp_load)'''

        pk, ob, *_ = SchemaUtils.extract_keys_and_indexes_from_ddl(ddl)
        
        assert pk == "timestamp_load", f"Expected 'timestamp_load', got '{pk}'"
        assert ob == "timestamp_load", f"Expected 'timestamp_load', got '{ob}'"

    def test_order_by_without_parentheses_single_column(self):
        """Test ORDER BY without parentheses (single column)."""
        ddl = '''CREATE TABLE test_org.no_paren
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
        ddl = '''CREATE TABLE test_org.with_ttl
(
    `timestamp` DateTime64(3, 'UTC'),
    `timestamp_load` DateTime64(3, 'UTC')
)
ENGINE = MergeTree()
PARTITION BY toYYYYMMDD(timestamp_load)
PRIMARY KEY timestamp_load
ORDER BY timestamp_load
TTL toDateTime(timestamp) + toIntervalDay(90)'''

        pk, ob, ttl_val, *_ = SchemaUtils.extract_keys_and_indexes_from_ddl(ddl)[0:3]
        
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
ORDER BY ()
TTL toDateTime(timestamp) + toIntervalDay(30)'''

        pk, ob, *_ = SchemaUtils.extract_keys_and_indexes_from_ddl(ddl)
        
        assert pk == "event_id, timestamp", f"Expected 'event_id, timestamp', got '{pk}'"
        # Empty ORDER BY should return None or empty string
        assert ob is None or ob == "", f"Empty ORDER BY should be None or '', got '{ob}'"


class TestOrderByExtractionWithProjection:
    """Test ORDER BY extraction when PROJECTION is present (bug fix validation)."""

    def test_order_by_with_single_projection_parentheses(self):
        """Test ORDER BY with parentheses + single PROJECTION - should extract table ORDER BY."""
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
        
        # BUG: This was extracting "timestamp" from PROJECTION instead of table-level ORDER BY
        assert pk == "timestamp_load, source_table, detection_time", \
            f"PRIMARY KEY failed: expected 'timestamp_load, source_table, detection_time', got '{pk}'"
        assert ob == "timestamp_load, source_table, detection_time", \
            f"ORDER BY failed: expected 'timestamp_load, source_table, detection_time', got '{ob}' (PROJECTION ORDER BY extracted by mistake?)"

    def test_order_by_with_single_projection_no_parentheses(self):
        """Test ORDER BY without parentheses + single PROJECTION."""
        ddl = '''CREATE TABLE test_org.logs_beats_filebeat
(
    `timestamp` DateTime64(3, 'UTC') CODEC(DoubleDelta, LZ4),
    `timestamp_load` DateTime64(3, 'UTC') CODEC(DoubleDelta, LZ4),
    `message` String CODEC(LZ4),
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
        
        assert pk == "timestamp_load", f"Expected 'timestamp_load', got '{pk}'"
        assert ob == "timestamp_load", f"Expected 'timestamp_load', got '{ob}' (PROJECTION ORDER BY extracted by mistake?)"

    def test_order_by_with_multiple_projections(self):
        """Test ORDER BY with multiple PROJECTION blocks."""
        ddl = '''CREATE TABLE test_org.multi_projection
(
    `timestamp` DateTime64(3, 'UTC'),
    `timestamp_load` DateTime64(3, 'UTC'),
    `user_id` String,
    `event_type` String,
    INDEX idx_user user_id TYPE bloom_filter GRANULARITY 4,
    PROJECTION by_timestamp
    (
        SELECT *
        ORDER BY timestamp
    ),
    PROJECTION by_user
    (
        SELECT *
        ORDER BY user_id, timestamp
    ),
    PROJECTION by_event
    (
        SELECT *
        ORDER BY event_type, timestamp_load
    )
)
ENGINE = SharedMergeTree('/clickhouse/tables/{uuid}/{shard}', '{replica}')
PARTITION BY toYYYYMMDD(timestamp_load)
PRIMARY KEY (timestamp_load, event_type, user_id)
ORDER BY (timestamp_load, event_type, user_id)
SAMPLE BY cityHash64(user_id)
TTL toDateTime(timestamp) + toIntervalDay(90)'''

        pk, ob, *_ = SchemaUtils.extract_keys_and_indexes_from_ddl(ddl)
        
        assert pk == "timestamp_load, event_type, user_id", \
            f"Expected 'timestamp_load, event_type, user_id', got '{pk}'"
        assert ob == "timestamp_load, event_type, user_id", \
            f"Expected 'timestamp_load, event_type, user_id', got '{ob}' (one of the PROJECTION ORDER BYs extracted by mistake?)"


class TestRealWorldScenarios:
    """Test with real-world DDL that was failing in production."""

    def test_real_world_logs_alerts_full(self):
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
        
        # THE BUG: ORDER BY was being extracted as "timestamp" (from PROJECTION) 
        # instead of "timestamp_load, source_table, detection_time" (from table level)
        assert pk == "timestamp_load, source_table, detection_time", \
            f"PRIMARY KEY failed: expected 'timestamp_load, source_table, detection_time', got '{pk}'"
        assert ob == "timestamp_load, source_table, detection_time", \
            f"ORDER BY failed: expected 'timestamp_load, source_table, detection_time', got '{ob}' - PROJECTION ORDER BY was extracted instead!"
        
        # Verify other extractions still work
        assert partition_by == "toYYYYMMDD(timestamp_load)"
        assert len(indexes) == 1
        assert indexes[0][0] == "idx_timestamp"
        assert projection is not None
        assert projection[0] == "timestamp_optimized"

    def test_real_world_logs_beats_filebeat_full(self):
        """Test with real-world logs_beats_filebeat schema."""
        ddl = '''CREATE TABLE test_org_08102025.logs_beats_filebeat
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
    `logjson` String CODEC(ZSTD(1)),
    `org_id` LowCardinality(String) CODEC(LZ4),
    `message` String CODEC(LZ4),
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
TTL toDateTime(timestamp) + toIntervalDay(90) WHERE timestamp IS NOT NULL, toDateTime(timestamp_load) + toIntervalDay(90) WHERE timestamp_load IS NOT NULL
SETTINGS index_granularity = 2048, ttl_only_drop_parts = 1'''

        pk, ob, *_ = SchemaUtils.extract_keys_and_indexes_from_ddl(ddl)
        
        assert pk == "timestamp_load", f"Expected 'timestamp_load', got '{pk}'"
        assert ob == "timestamp_load", f"Expected 'timestamp_load', got '{ob}' - PROJECTION ORDER BY was extracted instead!"


if __name__ == "__main__":
    # Allow running tests directly
    pytest.main([__file__, "-v"])
