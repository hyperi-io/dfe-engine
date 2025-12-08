import pytest
import logging
import os
import tempfile
from pathlib import Path
from dfe_engine.schema.schema_ch import ClickHouseSchema
import pandas as pd

logger = logging.getLogger(__name__)


@pytest.fixture
def temp_schema_files():
    """Create temporary schema files for testing."""
    temp_dir = tempfile.mkdtemp()

    meta_schema_data = {
        "column": ["event_id", "timestamp", "rule_id"],
        "type": ["string", "datetime", "uuid"],
        "index_type": ["", "range", ""],
        "index_order": [1, "", 2],
        "os_order": [1, "", 2],
        "comment": ["Event ID", "Event timestamp", "Rule ID"],
    }
    meta_schema_df = pd.DataFrame(meta_schema_data)
    meta_schema_path = os.path.join(temp_dir, "meta_schema.csv")
    meta_schema_df.to_csv(meta_schema_path, index=False)

    derived_schema_data = {
        "column": ["event_id", "rule_id"],
        "type": ["", ""],
        "index_type": ["", ""],
        "index_order": [1, 2],
        "os_order": [1, 2],
        "comment": ["Event ID", "Rule ID"],
    }
    derived_schema_df = pd.DataFrame(derived_schema_data)
    derived_schema_path = os.path.join(temp_dir, "derived_schema.csv")
    derived_schema_df.to_csv(derived_schema_path, index=False)

    additional_fields_data = {
        "column": ["severity_level"],
        "type": ["string_fast_lowcardinality"],
        "index_type": ["dimension"],
        "index_order": [3],
        "os_order": [3],
        "comment": ["Severity level"],
        "default": [""],
    }
    additional_fields_df = pd.DataFrame(additional_fields_data)
    additional_fields_path = os.path.join(temp_dir, "additional_fields.csv")
    additional_fields_df.to_csv(additional_fields_path, index=False)

    return {
        "temp_dir": temp_dir,
        "meta_schema_path": meta_schema_path,
        "derived_schema_path": derived_schema_path,
        "additional_fields_path": additional_fields_path,
    }


@pytest.fixture
def clickhouse_schema(temp_schema_files):
    """Create a ClickHouseSchema instance for testing."""
    output_dir = os.path.join(temp_schema_files["temp_dir"], "output")
    schema = ClickHouseSchema(
        name="test_comprehensive_schema",
        version="1.0.0",
        meta_schema_file_path=temp_schema_files["meta_schema_path"],
        common_resource_path="common/v001_001_008",
        derived_schema_full_path=temp_schema_files["derived_schema_path"],
        additional_fields_full_path=temp_schema_files["additional_fields_path"],
        dfe_output_path=output_dir,
        use_subsampling_feature=False,
        logger=logger,
    )
    return schema


@pytest.fixture
def clickhouse_schema_with_subsampling(temp_schema_files):
    """Create a ClickHouseSchema instance with subsampling for testing."""
    output_dir = os.path.join(temp_schema_files["temp_dir"], "output_subsampling")
    schema = ClickHouseSchema(
        name="test_comprehensive_schema_subsampling",
        version="1.0.0",
        meta_schema_file_path=temp_schema_files["meta_schema_path"],
        common_resource_path="common/v001_001_008",
        derived_schema_full_path=temp_schema_files["derived_schema_path"],
        additional_fields_full_path=temp_schema_files["additional_fields_path"],
        dfe_output_path=output_dir,
        use_subsampling_feature=True,
        logger=logger,
    )
    return schema


def test_partition_by_timestamp_load(clickhouse_schema):
    """Test that PARTITION BY uses toYYYYMMDD(timestamp_load)."""
    clickhouse_schema.build_clickhouse_schema()

    sql_file = Path(clickhouse_schema.schema_output_path) / "test_comprehensive_schema.sql"
    sql_content = sql_file.read_text()

    assert "PARTITION BY toYYYYMMDD(timestamp_load)" in sql_content, (
        "Schema should use PARTITION BY toYYYYMMDD(timestamp_load)"
    )


def test_order_by_starts_with_timestamp_load(clickhouse_schema):
    """Test that ORDER BY starts with timestamp_load."""
    clickhouse_schema.build_clickhouse_schema()

    sql_file = Path(clickhouse_schema.schema_output_path) / "test_comprehensive_schema.sql"
    sql_content = sql_file.read_text()

    lines = sql_content.split("\n")
    order_by_line = None
    for line in lines:
        if "ORDER BY" in line and "PROJECTION" not in line:
            order_by_line = line.strip()
            break

    assert order_by_line is not None, "ORDER BY clause should be present"
    assert "ORDER BY (timestamp_load," in order_by_line, "ORDER BY should start with timestamp_load"


def test_order_by_with_subsampling(clickhouse_schema_with_subsampling):
    """Test that ORDER BY with subsampling starts with cityHash64(timestamp_load), timestamp_load."""
    clickhouse_schema_with_subsampling.build_clickhouse_schema()

    sql_file = (
        Path(clickhouse_schema_with_subsampling.schema_output_path)
        / "test_comprehensive_schema_subsampling.sql"
    )
    sql_content = sql_file.read_text()

    lines = sql_content.split("\n")
    order_by_line = None
    for line in lines:
        if "ORDER BY" in line and "PROJECTION" not in line:
            order_by_line = line.strip()
            break

    assert order_by_line is not None, "ORDER BY clause should be present"
    assert "ORDER BY (cityHash64(timestamp_load), timestamp_load," in order_by_line, (
        "ORDER BY should start with cityHash64(timestamp_load), timestamp_load when subsampling is enabled"
    )


def test_timestamp_index_present(clickhouse_schema):
    """Test that timestamp index is present with correct granularity."""
    clickhouse_schema.build_clickhouse_schema()

    sql_file = Path(clickhouse_schema.schema_output_path) / "test_comprehensive_schema.sql"
    sql_content = sql_file.read_text()

    assert "INDEX idx_timestamp timestamp TYPE" in sql_content, "Timestamp index should be present"
    assert "GRANULARITY" in sql_content, "Index should have granularity specified"


def test_timestamp_projection_present(clickhouse_schema):
    """Test that timestamp projection is present."""
    clickhouse_schema.build_clickhouse_schema()

    sql_file = Path(clickhouse_schema.schema_output_path) / "test_comprehensive_schema.sql"
    sql_content = sql_file.read_text()

    assert "PROJECTION timestamp_optimized" in sql_content, "Timestamp projection should be present"
    assert "SELECT * ORDER BY timestamp" in sql_content, "Projection should order by timestamp"


def test_table_settings_present(clickhouse_schema):
    """Test that table settings are present."""
    clickhouse_schema.build_clickhouse_schema()

    sql_file = Path(clickhouse_schema.schema_output_path) / "test_comprehensive_schema.sql"
    sql_content = sql_file.read_text()

    assert "SETTINGS" in sql_content, "SETTINGS section should be present"
    assert "index_granularity = 2048" in sql_content, "index_granularity should be set to 2048"
    assert "ttl_only_drop_parts = 1" in sql_content, "ttl_only_drop_parts should be set to 1"


def test_sample_by_with_subsampling(clickhouse_schema_with_subsampling):
    """Test that SAMPLE BY is present when subsampling is enabled."""
    clickhouse_schema_with_subsampling.build_clickhouse_schema()

    sql_file = (
        Path(clickhouse_schema_with_subsampling.schema_output_path)
        / "test_comprehensive_schema_subsampling.sql"
    )
    sql_content = sql_file.read_text()

    assert "SAMPLE BY cityHash64(timestamp_load)" in sql_content, (
        "SAMPLE BY clause should be present when subsampling is enabled"
    )


def test_all_required_features_together(clickhouse_schema):
    """Test that all 5 required features are present together."""
    clickhouse_schema.build_clickhouse_schema()

    sql_file = Path(clickhouse_schema.schema_output_path) / "test_comprehensive_schema.sql"
    sql_content = sql_file.read_text()

    assert "PARTITION BY toYYYYMMDD(timestamp_load)" in sql_content

    order_by_found = False
    for line in sql_content.split("\n"):
        if "ORDER BY" in line and "PROJECTION" not in line:
            assert "timestamp_load" in line
            order_by_found = True
            break
    assert order_by_found, "ORDER BY clause should be present"

    assert "INDEX idx_timestamp timestamp TYPE" in sql_content
    assert "GRANULARITY" in sql_content

    assert "PROJECTION timestamp_optimized" in sql_content
    assert "SELECT * ORDER BY timestamp" in sql_content

    assert "SETTINGS" in sql_content
    assert "index_granularity = 2048" in sql_content
    assert "ttl_only_drop_parts = 1" in sql_content


def test_all_required_features_with_subsampling(clickhouse_schema_with_subsampling):
    """Test that all features work correctly with subsampling enabled."""
    clickhouse_schema_with_subsampling.build_clickhouse_schema()

    sql_file = (
        Path(clickhouse_schema_with_subsampling.schema_output_path)
        / "test_comprehensive_schema_subsampling.sql"
    )
    sql_content = sql_file.read_text()

    assert "PARTITION BY toYYYYMMDD(timestamp_load)" in sql_content
    assert "ORDER BY (cityHash64(timestamp_load), timestamp_load," in sql_content
    assert "INDEX idx_timestamp timestamp TYPE" in sql_content
    assert "GRANULARITY" in sql_content
    assert "PROJECTION timestamp_optimized" in sql_content
    assert "SETTINGS" in sql_content
    assert "index_granularity = 2048" in sql_content
    assert "ttl_only_drop_parts = 1" in sql_content
    assert "SAMPLE BY cityHash64(timestamp_load)" in sql_content


def test_dimension_index_creation(clickhouse_schema):
    """Test that dimension indexes are created correctly."""
    clickhouse_schema.build_clickhouse_schema()

    sql_file = Path(clickhouse_schema.schema_output_path) / "test_comprehensive_schema.sql"
    sql_content = sql_file.read_text()

    assert "INDEX idx_severity_level severity_level TYPE set(0) GRANULARITY 4" in sql_content, (
        "Dimension index should be created for severity_level"
    )


def test_no_duplicate_indexes(clickhouse_schema):
    """Test that there are no duplicate index names."""
    clickhouse_schema.build_clickhouse_schema()

    sql_file = Path(clickhouse_schema.schema_output_path) / "test_comprehensive_schema.sql"
    sql_content = sql_file.read_text()

    index_names = []
    for line in sql_content.split("\n"):
        if "INDEX idx_" in line:
            parts = line.strip().split()
            for _i, part in enumerate(parts):
                if part.startswith("idx_"):
                    index_names.append(part)
                    break

    unique_names = set(index_names)
    assert len(index_names) == len(unique_names), (
        f"Duplicate index names found: {[name for name in index_names if index_names.count(name) > 1]}"
    )


def test_schema_structure_order(clickhouse_schema):
    """Test that schema elements appear in the correct order."""
    clickhouse_schema.build_clickhouse_schema()

    sql_file = Path(clickhouse_schema.schema_output_path) / "test_comprehensive_schema.sql"
    sql_content = sql_file.read_text()

    lines = sql_content.split("\n")

    create_table_pos = -1
    engine_pos = -1
    partition_pos = -1
    primary_key_pos = -1
    order_by_pos = -1
    ttl_pos = -1
    settings_pos = -1

    for i, line in enumerate(lines):
        if "CREATE TABLE" in line:
            create_table_pos = i
        elif "ENGINE =" in line:
            engine_pos = i
        elif "PARTITION BY" in line:
            partition_pos = i
        elif "PRIMARY KEY" in line:
            primary_key_pos = i
        elif "ORDER BY" in line and "PROJECTION" not in line:
            order_by_pos = i
        elif "TTL timestamp" in line and "INTERVAL" in line:
            ttl_pos = i
        elif "SETTINGS" in line:
            settings_pos = i

    assert create_table_pos < engine_pos, "CREATE TABLE should come before ENGINE"
    assert engine_pos < partition_pos, "ENGINE should come before PARTITION BY"
    assert partition_pos < primary_key_pos, "PARTITION BY should come before PRIMARY KEY"
    assert primary_key_pos < order_by_pos, "PRIMARY KEY should come before ORDER BY"
    assert order_by_pos < ttl_pos, "ORDER BY should come before TTL"
    assert ttl_pos < settings_pos, "TTL should come before SETTINGS"


def test_partition_by_edge_cases(clickhouse_schema):
    """Test edge cases for PARTITION BY toYYYYMMDD(timestamp_load)."""
    clickhouse_schema.build_clickhouse_schema()

    sql_file = Path(clickhouse_schema.schema_output_path) / "test_comprehensive_schema.sql"
    sql_content = sql_file.read_text()

    assert "PARTITION BY toYYYYMMDD(timestamp_load)" in sql_content, (
        "PARTITION BY should use exact toYYYYMMDD(timestamp_load) format"
    )

    assert "toYYYYMM(" not in sql_content, "Should not use old toYYYYMM format"

    assert "toYYYYMMDD(timestamp)" not in sql_content, (
        "Should use timestamp_load, not timestamp for partitioning"
    )

    assert "partition by" not in sql_content.lower() or "PARTITION BY" in sql_content, (
        "PARTITION BY should be in uppercase"
    )

    assert "toYYYYMMDD( timestamp_load )" not in sql_content, (
        "Function should not have extra spaces"
    )


def test_order_by_timestamp_load_first_edge_cases(clickhouse_schema):
    """Test edge cases for ORDER BY starting with timestamp_load."""
    clickhouse_schema.build_clickhouse_schema()

    sql_file = Path(clickhouse_schema.schema_output_path) / "test_comprehensive_schema.sql"
    sql_content = sql_file.read_text()

    order_by_line = None
    for line in sql_content.split("\n"):
        if "ORDER BY" in line and "PROJECTION" not in line:
            order_by_line = line.strip()
            break

    assert order_by_line is not None, "ORDER BY clause should be present"

    order_by_part = order_by_line.split("ORDER BY")[1].strip()
    order_by_columns = order_by_part.strip("()").split(",")
    first_column = order_by_columns[0].strip()

    assert "timestamp_load" in first_column, (
        f"timestamp_load should be first in ORDER BY, but first column is: {first_column}"
    )

    assert len(order_by_columns) > 1, (
        "ORDER BY should have multiple columns, not just timestamp_load"
    )

    assert (
        not first_column.strip().startswith("timestamp)")
        and not first_column.strip() == "timestamp"
    ), "ORDER BY should start with timestamp_load, not plain timestamp"


def test_order_by_with_subsampling_edge_cases(clickhouse_schema_with_subsampling):
    """Test edge cases for ORDER BY with subsampling (cityHash64)."""
    clickhouse_schema_with_subsampling.build_clickhouse_schema()

    sql_file = (
        Path(clickhouse_schema_with_subsampling.schema_output_path)
        / "test_comprehensive_schema_subsampling.sql"
    )
    sql_content = sql_file.read_text()

    order_by_line = None
    for line in sql_content.split("\n"):
        if "ORDER BY" in line and "PROJECTION" not in line:
            order_by_line = line.strip()
            break

    assert order_by_line is not None, "ORDER BY clause should be present"

    assert "ORDER BY (cityHash64(timestamp_load)" in order_by_line, (
        "With subsampling, ORDER BY should start with cityHash64(timestamp_load)"
    )

    timestamp_load_count = order_by_line.count("timestamp_load")
    assert timestamp_load_count >= 2, (
        f"timestamp_load should appear at least twice in ORDER BY with subsampling, found {timestamp_load_count}"
    )

    assert "cityHash64( timestamp_load )" not in order_by_line, (
        "cityHash64 function should not have extra spaces"
    )


def test_skipping_index_timestamp_edge_cases(clickhouse_schema):
    """Test edge cases for timestamp skipping index."""
    clickhouse_schema.build_clickhouse_schema()

    sql_file = Path(clickhouse_schema.schema_output_path) / "test_comprehensive_schema.sql"
    sql_content = sql_file.read_text()

    assert "INDEX idx_timestamp timestamp TYPE" in sql_content, (
        "Index should be on 'timestamp' field with name 'idx_timestamp'"
    )

    assert "INDEX idx_timestamp timestamp TYPE set(0)" in sql_content, (
        "Index should use TYPE set(0) for datetime range index"
    )

    timestamp_index_line = None
    for line in sql_content.split("\n"):
        if "INDEX idx_timestamp timestamp TYPE" in line:
            timestamp_index_line = line.strip()
            break

    assert timestamp_index_line is not None, "Timestamp index line should be found"
    assert "GRANULARITY 4" in timestamp_index_line, (
        f"Index should have GRANULARITY 4, but line is: {timestamp_index_line}"
    )

    assert "INDEX idx_timestamp" in sql_content and "INDEX idx_timestamp_" not in sql_content, (
        "Index should be named exactly 'idx_timestamp'"
    )

    assert "INDEX idx_timestamp_load" not in sql_content, (
        "Should not create skipping index on timestamp_load"
    )


def test_projection_timestamp_edge_cases(clickhouse_schema):
    """Test edge cases for timestamp projection."""
    clickhouse_schema.build_clickhouse_schema()

    sql_file = Path(clickhouse_schema.schema_output_path) / "test_comprehensive_schema.sql"
    sql_content = sql_file.read_text()

    assert "PROJECTION timestamp_optimized" in sql_content, (
        "Projection should be named 'timestamp_optimized'"
    )

    assert "SELECT * ORDER BY timestamp" in sql_content, (
        "Projection should use 'SELECT * ORDER BY timestamp'"
    )

    assert "SELECT * ORDER BY timestamp_load" not in sql_content, (
        "Projection should order by 'timestamp', not 'timestamp_load'"
    )

    projection_line = None
    lines = sql_content.split("\n")
    for _i, line in enumerate(lines):
        if "PROJECTION timestamp_optimized" in line:
            projection_line = line.strip()
            break

    assert projection_line is not None, "Projection definition should be found"

    normalized_line = projection_line.replace(" ", "").replace("\n", "")
    expected_elements = [
        "PROJECTIONtimestamp_optimized",
        "(",
        "SELECT",
        "*",
        "ORDER",
        "BY",
        "timestamp",
        ")",
    ]

    for element in expected_elements:
        assert element in normalized_line, (
            f"Projection should contain '{element}', but found: {projection_line}"
        )


def test_table_settings_edge_cases(clickhouse_schema):
    """Test edge cases for table settings."""
    clickhouse_schema.build_clickhouse_schema()

    sql_file = Path(clickhouse_schema.schema_output_path) / "test_comprehensive_schema.sql"
    sql_content = sql_file.read_text()

    assert "SETTINGS" in sql_content, "Should have SETTINGS section"

    assert "index_granularity = 2048" in sql_content, "index_granularity should be set to 2048"

    assert "ttl_only_drop_parts = 1" in sql_content, "ttl_only_drop_parts should be set to 1"

    assert "index_granularity = 8192" not in sql_content, "Should not use default granularity 8192"

    settings_start = sql_content.find("SETTINGS")
    settings_section = (
        sql_content[settings_start : settings_start + 200] if settings_start != -1 else ""
    )

    assert settings_start != -1, "SETTINGS section should be found"
    assert "index_granularity = 2048" in settings_section, (
        f"Settings section should contain index_granularity = 2048, but found: {settings_section}"
    )

    assert "index_granularity" in settings_section and "ttl_only_drop_parts" in settings_section, (
        "Both required settings should be present in SETTINGS section"
    )


def test_feature_interaction_edge_cases(clickhouse_schema):
    """Test edge cases for interactions between the 5 features."""
    clickhouse_schema.build_clickhouse_schema()

    sql_file = Path(clickhouse_schema.schema_output_path) / "test_comprehensive_schema.sql"
    sql_content = sql_file.read_text()

    features_present = {
        "partition": "PARTITION BY toYYYYMMDD(timestamp_load)" in sql_content,
        "order_by": "ORDER BY (timestamp_load" in sql_content,
        "index": "INDEX idx_timestamp timestamp TYPE set(0)" in sql_content,
        "projection": "PROJECTION timestamp_optimized" in sql_content,
        "settings": "SETTINGS" in sql_content and "index_granularity = 2048" in sql_content,
    }

    missing_features = [name for name, present in features_present.items() if not present]
    assert len(missing_features) == 0, f"Missing features: {missing_features}"

    partition_uses_timestamp_load = "PARTITION BY toYYYYMMDD(timestamp_load)" in sql_content
    order_by_uses_timestamp_load = "ORDER BY (timestamp_load" in sql_content
    index_uses_timestamp = "INDEX idx_timestamp timestamp TYPE" in sql_content
    projection_uses_timestamp = "SELECT * ORDER BY timestamp" in sql_content

    assert partition_uses_timestamp_load, "PARTITION BY should use timestamp_load"
    assert order_by_uses_timestamp_load, "ORDER BY should start with timestamp_load"
    assert index_uses_timestamp, "INDEX should use timestamp"
    assert projection_uses_timestamp, "PROJECTION should use timestamp"

    assert "PARTITION BY toYYYYMMDD(timestamp)" not in sql_content, (
        "PARTITION should not use plain timestamp"
    )
    assert "INDEX idx_timestamp timestamp_load" not in sql_content, (
        "INDEX should not use timestamp_load"
    )


def test_subsampling_feature_complete_edge_cases(clickhouse_schema_with_subsampling):
    """Test complete edge cases for subsampling feature integration."""
    clickhouse_schema_with_subsampling.build_clickhouse_schema()

    sql_file = (
        Path(clickhouse_schema_with_subsampling.schema_output_path)
        / "test_comprehensive_schema_subsampling.sql"
    )
    sql_content = sql_file.read_text()

    assert "SAMPLE BY cityHash64(timestamp_load)" in sql_content, (
        "SAMPLE BY should use cityHash64(timestamp_load)"
    )

    assert "ORDER BY (cityHash64(timestamp_load), timestamp_load" in sql_content, (
        "ORDER BY should start with cityHash64(timestamp_load), timestamp_load"
    )

    features_with_subsampling = {
        "partition": "PARTITION BY toYYYYMMDD(timestamp_load)" in sql_content,
        "order_by": "ORDER BY (cityHash64(timestamp_load), timestamp_load" in sql_content,
        "index": "INDEX idx_timestamp timestamp TYPE set(0)" in sql_content,
        "projection": "PROJECTION timestamp_optimized" in sql_content,
        "settings": "SETTINGS" in sql_content and "index_granularity = 2048" in sql_content,
        "sample_by": "SAMPLE BY cityHash64(timestamp_load)" in sql_content,
    }

    missing_features = [name for name, present in features_with_subsampling.items() if not present]
    assert len(missing_features) == 0, f"Missing features with subsampling: {missing_features}"

    cityhash_count = sql_content.count("cityHash64(timestamp_load)")
    assert cityhash_count >= 2, (
        f"cityHash64(timestamp_load) should appear at least twice (ORDER BY and SAMPLE BY), found {cityhash_count}"
    )


def test_granularity_consistency_edge_cases(clickhouse_schema):
    """Test edge cases for granularity consistency across features."""
    clickhouse_schema.build_clickhouse_schema()

    sql_file = Path(clickhouse_schema.schema_output_path) / "test_comprehensive_schema.sql"
    sql_content = sql_file.read_text()

    assert "GRANULARITY 4" in sql_content, "Range index should use GRANULARITY 4"

    assert "index_granularity = 2048" in sql_content, (
        "Table setting index_granularity should be 2048"
    )

    assert "index_granularity = 8192" not in sql_content, (
        "Should not have default index_granularity"
    )

    range_index_granularity_found = "GRANULARITY 4" in sql_content
    table_granularity_found = "index_granularity = 2048" in sql_content

    assert range_index_granularity_found, "Range indexes should use GRANULARITY 4"
    assert table_granularity_found, "Table should use index_granularity = 2048"


def test_sql_syntax_edge_cases(clickhouse_schema):
    """Test edge cases for SQL syntax correctness."""
    clickhouse_schema.build_clickhouse_schema()

    sql_file = Path(clickhouse_schema.schema_output_path) / "test_comprehensive_schema.sql"
    sql_content = sql_file.read_text()

    open_parens = sql_content.count("(")
    close_parens = sql_content.count(")")
    assert open_parens == close_parens, (
        f"Parentheses should be balanced: {open_parens} open, {close_parens} close"
    )

    assert ",)" not in sql_content, "Should not have trailing commas"

    sql_keywords = [
        "CREATE",
        "TABLE",
        "ENGINE",
        "PARTITION",
        "BY",
        "ORDER",
        "INDEX",
        "TYPE",
        "GRANULARITY",
        "PROJECTION",
        "SELECT",
        "SETTINGS",
    ]
    for keyword in sql_keywords:
        if keyword.lower() in sql_content.lower():
            lowercase_count = sql_content.count(keyword.lower())
            uppercase_count = sql_content.count(keyword.upper())
            assert uppercase_count >= lowercase_count, (
                f"Keyword {keyword} should be in uppercase more often than lowercase"
            )

    assert sql_content.strip().endswith(";"), "SQL should end with semicolon"
