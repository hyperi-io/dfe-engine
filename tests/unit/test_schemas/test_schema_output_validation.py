"""
Schema Output Validation Tests

This module captures expected schema outputs BEFORE any refactoring,
then validates that refactored code produces IDENTICAL output.

GOLDEN REFERENCE POLICY:
========================
The files in expected_outputs/ are our GOLDEN REFERENCE. They capture
the correct output from BEFORE any code changes.

WHEN TO UPDATE BASELINES:
- BUG FIX: If current output is WRONG (e.g., nested properties bug),
  fix the code AND update baseline to the CORRECT output
- NEW FEATURE: Add NEW baseline tests for new functionality

WHEN NOT TO UPDATE BASELINES:
- Refactoring: Output must remain IDENTICAL
- Performance optimization: Output must remain IDENTICAL
- Code cleanup: Output must remain IDENTICAL

Usage:
1. Run with GENERATE_BASELINE=true to create expected_outputs/ directory
2. After refactoring, run normally to compare against baseline

Example:
    GENERATE_BASELINE=true uv run pytest tests/unit_tests/test_schemas/test_schema_output_validation.py -v
"""

import logging
import os
import json
import hashlib
from pathlib import Path
from typing import Any, Optional
import pytest
import pandas as pd

# Baseline generation flag - set via environment variable
GENERATE_BASELINE = os.environ.get("GENERATE_BASELINE", "false").lower() == "true"
BASELINE_DIR = Path(__file__).parent / "expected_outputs"

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)


def save_baseline(name: str, content: Any, content_type: str = "text"):
    """Save baseline output for comparison."""
    BASELINE_DIR.mkdir(exist_ok=True)

    if content_type == "json":
        filepath = BASELINE_DIR / f"{name}.json"
        with open(filepath, "w") as f:
            json.dump(content, f, indent=2, sort_keys=True, default=str)
    elif content_type == "csv":
        filepath = BASELINE_DIR / f"{name}.csv"
        if isinstance(content, pd.DataFrame):
            content.to_csv(filepath, index=False)
        else:
            with open(filepath, "w") as f:
                f.write(content)
    else:
        filepath = BASELINE_DIR / f"{name}.txt"
        with open(filepath, "w") as f:
            f.write(str(content))

    logger.info(f"Saved baseline: {filepath}")
    return filepath


def load_baseline(name: str, content_type: str = "text") -> Optional[Any]:
    """Load baseline output for comparison."""
    if content_type == "json":
        filepath = BASELINE_DIR / f"{name}.json"
    elif content_type == "csv":
        filepath = BASELINE_DIR / f"{name}.csv"
    else:
        filepath = BASELINE_DIR / f"{name}.txt"

    if not filepath.exists():
        return None

    if content_type == "json":
        with open(filepath) as f:
            return json.load(f)
    elif content_type == "csv":
        return pd.read_csv(filepath)
    else:
        with open(filepath) as f:
            return f.read()


def compare_dataframes(df1: pd.DataFrame, df2: pd.DataFrame, name: str) -> bool:
    """Compare two DataFrames for equality, with detailed diff on mismatch."""
    # Normalize empty strings and NaN for comparison
    df1_norm = df1.copy()
    df2_norm = df2.copy()

    # Normalize all columns: convert empty strings and NaN variants to a consistent representation
    for df in [df1_norm, df2_norm]:
        for col in df.columns:
            # Convert column to object type for consistent string comparison
            # This handles Int64, float64, and object columns uniformly
            col_values = df[col].astype(object)
            # Replace various forms of "empty" with empty string
            col_values = col_values.fillna('')  # Replace NaN/pd.NA/None with ''
            col_values = col_values.replace('nan', '')  # Replace string 'nan'
            # Convert to string for consistent comparison
            col_values = col_values.astype(str)
            col_values = col_values.replace('nan', '')  # Handle any remaining nan strings
            col_values = col_values.replace('<NA>', '')  # Handle pd.NA string representation
            # Normalize numeric strings: '1.0' -> '1', '2.0' -> '2'
            col_values = col_values.apply(lambda x: str(int(float(x))) if x and x.replace('.', '').replace('-', '').isdigit() else x)
            df[col] = col_values

    # Sort both by column to ensure consistent comparison
    df1_sorted = df1_norm.sort_values(by=list(df1_norm.columns)).reset_index(drop=True)
    df2_sorted = df2_norm.sort_values(by=list(df2_norm.columns)).reset_index(drop=True)

    if df1_sorted.equals(df2_sorted):
        return True

    # Log differences
    logger.error(f"DataFrame mismatch for {name}")
    logger.error(f"Shape: {df1.shape} vs {df2.shape}")

    if set(df1.columns) != set(df2.columns):
        logger.error(f"Column diff: {set(df1.columns) ^ set(df2.columns)}")

    # Show actual differences for debugging
    for col in df1_sorted.columns:
        if col in df2_sorted.columns:
            diff_mask = df1_sorted[col] != df2_sorted[col]
            if diff_mask.any():
                logger.error(f"Column '{col}' differs at rows: {list(diff_mask[diff_mask].index)}")
                logger.error(f"  Result:   {df1_sorted.loc[diff_mask, col].tolist()}")
                logger.error(f"  Baseline: {df2_sorted.loc[diff_mask, col].tolist()}")

    return False


class TestSchemaUtilOutputValidation:
    """Tests that validate SchemaUtils output remains consistent after refactoring."""

    def test_strip_duplicate_columns(self):
        """
        Test that duplicate column stripping works consistently.
        This is a core utility function that merges common and custom schema fields.
        """
        from dfe_engine.schema.schema_util import SchemaUtils

        meta_schema_df = pd.DataFrame({
            "column": ["custom_field", "timestamp", "org_id"],
            "type": ["string", "datetime", "string"],
            "default": ["", "", ""],
            "index_order": ["", "", ""],
            "index_type": ["", "", ""],
            "os_order": ["", "", ""],
            "comment": ["Custom", "Duplicate", "Duplicate"],
        })

        common_header_df = pd.DataFrame({
            "column": ["timestamp", "org_id", "timestamp_load"],
            "type": ["datetime", "string", "datetime"],
            "default": ["", "", ""],
            "index_order": ["", "", ""],
            "index_type": ["", "", ""],
            "os_order": ["", "", ""],
            "comment": ["Time", "Org", "Load time"],
        })

        result_df = SchemaUtils.strip_duplicate_config_columns_from_non_common_df(
            meta_schema_df.copy(),
            common_header_df.copy(),
            "test_schema",
            logger,
            is_ch_flag=True
        )

        test_name = "strip_duplicate_columns"

        if GENERATE_BASELINE:
            save_baseline(test_name, result_df, "csv")
            pytest.skip("Baseline generated - rerun to validate")

        baseline = load_baseline(test_name, "csv")
        if baseline is None:
            pytest.skip(f"No baseline found for {test_name}. Run with GENERATE_BASELINE=true")

        assert compare_dataframes(result_df, baseline, test_name), \
            f"strip_duplicate_columns output changed!"

    def test_flatten_properties_output(self):
        """
        Test that flatten_properties produces consistent output.
        This is used for processing OpenSearch/Elasticsearch templates.
        """
        from dfe_engine.schema.schema_util import SchemaUtils

        input_properties = {
            "container": {
                "properties": {
                    "cpu": {"properties": {"usage": {"type": "float"}}},
                    "name": {"type": "keyword"},
                }
            },
            "host": {"type": "keyword"},
        }

        result = SchemaUtils.flatten_properties(input_properties)

        test_name = "flatten_properties"

        if GENERATE_BASELINE:
            save_baseline(test_name, result, "json")
            pytest.skip("Baseline generated - rerun to validate")

        baseline = load_baseline(test_name, "json")
        if baseline is None:
            pytest.skip(f"No baseline found for {test_name}. Run with GENERATE_BASELINE=true")

        assert result == baseline, f"flatten_properties output changed!"

    def test_extract_field_paths_output(self):
        """
        Test that extract_field_paths produces consistent output.
        """
        from dfe_engine.schema.schema_util import SchemaUtils

        input_properties = {
            "user": {
                "properties": {
                    "id": {"type": "integer"},
                    "name": {"type": "keyword"},
                    "profile": {
                        "properties": {
                            "avatar": {"type": "text"},
                        }
                    }
                }
            },
            "timestamp": {"type": "date"},
        }

        result = SchemaUtils.extract_field_paths(input_properties)

        test_name = "extract_field_paths"

        if GENERATE_BASELINE:
            # Convert set to sorted list for consistent serialization
            save_baseline(test_name, sorted(list(result)), "json")
            pytest.skip("Baseline generated - rerun to validate")

        baseline = load_baseline(test_name, "json")
        if baseline is None:
            pytest.skip(f"No baseline found for {test_name}. Run with GENERATE_BASELINE=true")

        assert result == set(baseline), f"extract_field_paths output changed!"

    def test_sql_column_fix_name_output(self):
        """
        Test that sql_column_fix_name produces consistent output.
        This converts field names to valid ClickHouse column names.
        """
        from dfe_engine.schema.schema_util import SchemaUtils

        test_cases = [
            "o365.audit.Severity",
            "user-name",
            "host.cpu.usage",
            "EventId_123",
            "field.with.many.dots",
        ]

        results = {name: SchemaUtils.sql_column_fix_name(name) for name in test_cases}

        test_name = "sql_column_fix_name"

        if GENERATE_BASELINE:
            save_baseline(test_name, results, "json")
            pytest.skip("Baseline generated - rerun to validate")

        baseline = load_baseline(test_name, "json")
        if baseline is None:
            pytest.skip(f"No baseline found for {test_name}. Run with GENERATE_BASELINE=true")

        assert results == baseline, f"sql_column_fix_name output changed!"

    def test_dict_merge_output(self):
        """
        Test that dict_merge produces consistent output.
        Used for merging OpenSearch template properties.
        """
        from dfe_engine.schema.schema_util import SchemaUtils

        dict1 = {"a": 1, "b": {"c": 3, "d": {"e": 5}}}
        dict2 = {"b": {"d": {"f": 6}}, "g": 7}

        result = SchemaUtils.dict_merge(dict1.copy(), dict2.copy())

        test_name = "dict_merge"

        if GENERATE_BASELINE:
            save_baseline(test_name, result, "json")
            pytest.skip("Baseline generated - rerun to validate")

        baseline = load_baseline(test_name, "json")
        if baseline is None:
            pytest.skip(f"No baseline found for {test_name}. Run with GENERATE_BASELINE=true")

        assert result == baseline, f"dict_merge output changed!"

    def test_extract_keys_and_indexes_from_ddl(self):
        """
        Test DDL parsing produces consistent output.
        This is used for schema modification and comparison.
        """
        from dfe_engine.schema.schema_util import SchemaUtils

        ddl = """
        CREATE TABLE IF NOT EXISTS logs.test_table (
            timestamp DateTime,
            org_id LowCardinality(String),
            event_id String,
            INDEX idx_event_id (event_id) TYPE tokenbf_v1(10240, 3, 0) GRANULARITY 1
        )
        ENGINE = MergeTree()
        PARTITION BY toYYYYMMDD(timestamp)
        ORDER BY (timestamp, org_id)
        PRIMARY KEY (timestamp, org_id)
        TTL timestamp + INTERVAL 90 DAY DELETE WHERE timestamp >= 0
        SETTINGS index_granularity = 2048;
        """

        result = SchemaUtils.extract_keys_and_indexes_from_ddl(ddl)
        # Convert to serializable format
        primary_key, order_by, indexes, ttl, sample_by, partition_by, projection, settings = result

        # Convert tuples to lists for JSON serialization consistency
        result_dict = {
            "primary_key": primary_key,
            "order_by": order_by,
            "indexes": [list(idx) for idx in indexes] if indexes else [],
            "ttl": ttl,
            "sample_by": sample_by,
            "partition_by": partition_by,
            "projection": list(projection) if projection else None,
            "settings": settings,
        }

        test_name = "extract_keys_from_ddl"

        if GENERATE_BASELINE:
            save_baseline(test_name, result_dict, "json")
            pytest.skip("Baseline generated - rerun to validate")

        baseline = load_baseline(test_name, "json")
        if baseline is None:
            pytest.skip(f"No baseline found for {test_name}. Run with GENERATE_BASELINE=true")

        assert result_dict == baseline, f"extract_keys_and_indexes_from_ddl output changed!"

    def test_normalize_dot_version(self):
        """Test version normalization for schema versioning."""
        from dfe_engine.schema.schema_util import SchemaUtils

        test_cases = ["1.0.0", "1.2.3", "10.20.30", "1.0.10"]
        results = {v: SchemaUtils.normalize_dot_version(v) for v in test_cases}

        test_name = "normalize_dot_version"

        if GENERATE_BASELINE:
            save_baseline(test_name, results, "json")
            pytest.skip("Baseline generated - rerun to validate")

        baseline = load_baseline(test_name, "json")
        if baseline is None:
            pytest.skip(f"No baseline found for {test_name}. Run with GENERATE_BASELINE=true")

        assert results == baseline, f"normalize_dot_version output changed!"

    def test_flatten_properties_deeply_nested_filebeat(self):
        """
        Test flatten_properties with deeply nested structure matching real filebeat templates.

        This test validates the CRITICAL nested 'properties' pattern from Elastic/OpenSearch
        templates used in filebeat and other beats sources.

        Structure tested (from actual filebeat template):
        - Level 1: container
        - Level 2: container.image, container.disk, container.network
        - Level 3: container.image.name, container.disk.read, container.disk.write
        - Level 4: container.disk.read.bytes, container.network.ingress.bytes

        This ensures we correctly flatten deeply nested Elastic schema structures.
        """
        from dfe_engine.schema.schema_util import SchemaUtils

        # Exact structure from filebeat template (lines 107-191)
        filebeat_container_properties = {
            "container": {
                "properties": {
                    "image": {
                        "properties": {
                            "name": {
                                "type": "keyword",
                                "ignore_above": 1024,
                                "normalizer": "lowercase_normalizer"
                            },
                            "tag": {
                                "type": "keyword",
                                "ignore_above": 1024,
                                "normalizer": "lowercase_normalizer"
                            }
                        }
                    },
                    "disk": {
                        "properties": {
                            "read": {
                                "properties": {
                                    "bytes": {"type": "long"}
                                }
                            },
                            "write": {
                                "properties": {
                                    "bytes": {"type": "long"}
                                }
                            }
                        }
                    },
                    "memory": {
                        "properties": {
                            "usage": {"type": "double"}
                        }
                    },
                    "name": {
                        "type": "keyword",
                        "ignore_above": 1024,
                        "normalizer": "lowercase_normalizer"
                    },
                    "runtime": {
                        "type": "keyword",
                        "ignore_above": 1024,
                        "normalizer": "lowercase_normalizer"
                    },
                    "cpu": {
                        "properties": {
                            "usage": {"type": "double"}
                        }
                    },
                    "id": {
                        "type": "keyword",
                        "ignore_above": 1024,
                        "normalizer": "lowercase_normalizer"
                    },
                    "labels": {"type": "object"},
                    "network": {
                        "properties": {
                            "ingress": {
                                "properties": {
                                    "bytes": {"type": "long"}
                                }
                            },
                            "egress": {
                                "properties": {
                                    "bytes": {"type": "long"}
                                }
                            }
                        }
                    }
                }
            }
        }

        result = SchemaUtils.flatten_properties(filebeat_container_properties)

        test_name = "flatten_properties_filebeat_nested"

        if GENERATE_BASELINE:
            save_baseline(test_name, result, "json")
            pytest.skip("Baseline generated - rerun to validate")

        baseline = load_baseline(test_name, "json")
        if baseline is None:
            pytest.skip(f"No baseline found for {test_name}. Run with GENERATE_BASELINE=true")

        assert result == baseline, f"flatten_properties_filebeat_nested output changed!"

    def test_extract_field_paths_deeply_nested_filebeat(self):
        """
        Test extract_field_paths with deeply nested filebeat-style structure.

        This validates we correctly extract all flattened field paths from
        deeply nested Elastic/OpenSearch template structures.
        """
        from dfe_engine.schema.schema_util import SchemaUtils

        # Same structure as above for consistency
        filebeat_container_properties = {
            "container": {
                "properties": {
                    "image": {
                        "properties": {
                            "name": {"type": "keyword"},
                            "tag": {"type": "keyword"}
                        }
                    },
                    "disk": {
                        "properties": {
                            "read": {
                                "properties": {
                                    "bytes": {"type": "long"}
                                }
                            },
                            "write": {
                                "properties": {
                                    "bytes": {"type": "long"}
                                }
                            }
                        }
                    },
                    "memory": {
                        "properties": {
                            "usage": {"type": "double"}
                        }
                    },
                    "name": {"type": "keyword"},
                    "runtime": {"type": "keyword"},
                    "cpu": {
                        "properties": {
                            "usage": {"type": "double"}
                        }
                    },
                    "id": {"type": "keyword"},
                    "labels": {"type": "object"},
                    "network": {
                        "properties": {
                            "ingress": {
                                "properties": {
                                    "bytes": {"type": "long"}
                                }
                            },
                            "egress": {
                                "properties": {
                                    "bytes": {"type": "long"}
                                }
                            }
                        }
                    }
                }
            }
        }

        result = SchemaUtils.extract_field_paths(filebeat_container_properties)

        test_name = "extract_field_paths_filebeat_nested"

        if GENERATE_BASELINE:
            save_baseline(test_name, sorted(list(result)), "json")
            pytest.skip("Baseline generated - rerun to validate")

        baseline = load_baseline(test_name, "json")
        if baseline is None:
            pytest.skip(f"No baseline found for {test_name}. Run with GENERATE_BASELINE=true")

        assert result == set(baseline), f"extract_field_paths_filebeat_nested output changed!"

    def test_apply_derived_schema_output(self):
        """
        Test that apply_derived_schema produces consistent output.

        This is the CRITICAL function for merging meta schemas with sub-schemas.
        It handles:
        - Filtering columns based on sub-schema wildcards and exact matches
        - Overriding index_order, index_type, type, and default values
        - Removing parent fields when children exist

        Performance note: This function uses iterrows and apply which are slow.
        After vectorization optimization, output must remain IDENTICAL.
        """
        from dfe_engine.schema.schema_util import SchemaUtils

        # Create a realistic meta schema (parent schema)
        # Note: index_order must be nullable int (pd.NA for empty), not empty string
        meta_schema_df = pd.DataFrame({
            "column": [
                "timestamp", "org_id", "host", "host.name", "host.ip",
                "user", "user.name", "user.id", "event", "event.category",
                "event.action", "source", "source.ip", "source.port",
                "destination", "destination.ip", "destination.port"
            ],
            "type": [
                "datetime", "string", "object", "string", "ip_field",
                "object", "string", "string", "object", "string",
                "string", "object", "ip_field", "integer",
                "object", "ip_field", "integer"
            ],
            "default": [""] * 17,
            "index_order": pd.array([1, 2, pd.NA, pd.NA, pd.NA, pd.NA, pd.NA, pd.NA, pd.NA, pd.NA, pd.NA, pd.NA, pd.NA, pd.NA, pd.NA, pd.NA, pd.NA], dtype="Int64"),
            "index_type": ["dimension", "dimension", "", "", "", "", "", "", "", "", "", "", "", "", "", "", ""],
            "os_order": [""] * 17,
            "comment": ["Time", "Organization", "Host object", "Hostname", "Host IP",
                       "User object", "Username", "User ID", "Event object", "Event category",
                       "Event action", "Source object", "Source IP", "Source port",
                       "Dest object", "Dest IP", "Dest port"],
        })

        # Create a sub-schema that filters and overrides
        derived_schema_df = pd.DataFrame({
            "column": ["host.*", "user.name", "event.category", "source.ip"],
            "index_order": [pd.NA, 3, 4, 5],
            "index_type": ["", "dimension", "dimension", "dimension"],
            "type": ["", "", "", ""],
            "default": ["", "", "", ""],
        })

        result_df = SchemaUtils.apply_derived_schema(
            meta_schema_df.copy(),
            derived_schema_df.copy(),
            logger
        )

        test_name = "apply_derived_schema"

        if GENERATE_BASELINE:
            save_baseline(test_name, result_df, "csv")
            pytest.skip("Baseline generated - rerun to validate")

        baseline = load_baseline(test_name, "csv")
        if baseline is None:
            pytest.skip(f"No baseline found for {test_name}. Run with GENERATE_BASELINE=true")

        assert compare_dataframes(result_df, baseline, test_name), \
            f"apply_derived_schema output changed!"

    def test_apply_derived_schema_large_scale(self):
        """
        Test apply_derived_schema with a larger dataset simulating filebeat scale.

        Filebeat schemas can have 7000+ rows. This test validates consistent
        output at scale and provides a baseline for performance comparison.
        """
        from dfe_engine.schema.schema_util import SchemaUtils

        # Generate a moderately large schema (500 rows to keep test fast)
        columns = []
        types = []
        for prefix in ["host", "user", "event", "source", "destination", "network", "process", "file"]:
            for i in range(60):
                columns.append(f"{prefix}.field_{i}")
                types.append("string" if i % 3 == 0 else ("integer" if i % 3 == 1 else "ip_field"))

        # Add some top-level fields
        columns.extend(["timestamp", "org_id", "message", "tags"])
        types.extend(["datetime", "string", "string", "array"])

        # Build index_order with proper nullable Int64 type
        index_order_values = [pd.NA] * len(columns)
        meta_schema_df = pd.DataFrame({
            "column": columns,
            "type": types,
            "default": [""] * len(columns),
            "index_order": pd.array(index_order_values, dtype="Int64"),
            "index_type": [""] * len(columns),
            "os_order": [""] * len(columns),
            "comment": [""] * len(columns),
        })
        # Set first two as indexed
        meta_schema_df.loc[meta_schema_df['column'] == 'timestamp', 'index_order'] = 1
        meta_schema_df.loc[meta_schema_df['column'] == 'org_id', 'index_order'] = 2

        # Sub-schema filters to specific prefixes with wildcards
        derived_schema_df = pd.DataFrame({
            "column": ["host.*", "user.*", "event.*", "timestamp", "org_id"],
            "index_order": [pd.NA, pd.NA, pd.NA, 1, 2],
            "index_type": ["", "", "", "dimension", "dimension"],
        })

        result_df = SchemaUtils.apply_derived_schema(
            meta_schema_df.copy(),
            derived_schema_df.copy(),
            logger
        )

        test_name = "apply_derived_schema_large"

        if GENERATE_BASELINE:
            save_baseline(test_name, result_df, "csv")
            pytest.skip("Baseline generated - rerun to validate")

        baseline = load_baseline(test_name, "csv")
        if baseline is None:
            pytest.skip(f"No baseline found for {test_name}. Run with GENERATE_BASELINE=true")

        assert compare_dataframes(result_df, baseline, test_name), \
            f"apply_derived_schema_large output changed!"


class TestElasticEdgeCases:
    """
    Tests for Elastic/OpenSearch template edge cases that may break the converter.

    These tests document and validate handling of:
    1. `fields` with `properties` inside (OpenSearch/beats pattern)
    2. Standard multi-fields (`fields` with type directly)
    3. `nested` type fields
    4. `copy_to` parameter
    5. `enabled: false` fields
    6. `alias` type fields
    7. `flattened` type fields
    """

    def test_fields_with_properties_pattern(self):
        """
        Test the OpenSearch/beats pattern where 'fields' contains 'properties'.

        This is found 70+ times in filebeat template:
        ```
        "name": {
          "properties": {
            "fields": {
              "properties": {
                "text": {"type": "keyword"}
              }
            }
          }
        }
        ```

        Expected flattening:
        - observer.os.name.fields.text -> {"type": "keyword"}
        """
        from dfe_engine.schema.schema_util import SchemaUtils

        # Exact structure from filebeat observer.os.name
        properties = {
            "observer": {
                "properties": {
                    "os": {
                        "properties": {
                            "name": {
                                "properties": {
                                    "fields": {
                                        "properties": {
                                            "text": {
                                                "type": "keyword",
                                                "ignore_above": 1024,
                                                "normalizer": "lowercase_normalizer"
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }

        result = SchemaUtils.flatten_properties(properties)

        test_name = "fields_with_properties_pattern"

        if GENERATE_BASELINE:
            save_baseline(test_name, result, "json")
            pytest.skip("Baseline generated - rerun to validate")

        baseline = load_baseline(test_name, "json")
        if baseline is None:
            pytest.skip(f"No baseline found for {test_name}. Run with GENERATE_BASELINE=true")

        assert result == baseline, f"fields_with_properties_pattern output changed!"

    def test_standard_multifields(self):
        """
        Test standard Elastic multi-fields pattern.

        Standard pattern:
        ```
        "message": {
          "type": "text",
          "fields": {
            "keyword": {"type": "keyword", "ignore_above": 256},
            "english": {"type": "text", "analyzer": "english"}
          }
        }
        ```

        Expected: Multi-fields should be traversed and flattened to:
        - message -> {"type": "text"}
        - message.keyword -> {"type": "keyword", "ignore_above": 256}
        - message.english -> {"type": "text", "analyzer": "english"}
        """
        from dfe_engine.schema.schema_util import SchemaUtils

        properties = {
            "message": {
                "type": "text",
                "fields": {
                    "keyword": {
                        "type": "keyword",
                        "ignore_above": 256
                    },
                    "english": {
                        "type": "text",
                        "analyzer": "english"
                    }
                }
            },
            "host": {
                "type": "keyword"
            }
        }

        result = SchemaUtils.flatten_properties(properties)

        test_name = "standard_multifields"

        if GENERATE_BASELINE:
            save_baseline(test_name, result, "json")
            pytest.skip("Baseline generated - rerun to validate")

        baseline = load_baseline(test_name, "json")
        if baseline is None:
            pytest.skip(f"No baseline found for {test_name}. Run with GENERATE_BASELINE=true")

        assert result == baseline, f"standard_multifields output changed!"

    def test_nested_type_fields(self):
        """
        Test handling of 'nested' type fields.

        Pattern from filebeat:
        ```
        "AuditKeyValues": {
          "type": "nested"
        }
        ```

        Or with properties:
        ```
        "comments": {
          "type": "nested",
          "properties": {
            "author": {"type": "keyword"},
            "text": {"type": "text"}
          }
        }
        ```
        """
        from dfe_engine.schema.schema_util import SchemaUtils

        properties = {
            "simple_nested": {
                "type": "nested"
            },
            "nested_with_props": {
                "type": "nested",
                "properties": {
                    "author": {"type": "keyword"},
                    "text": {"type": "text"}
                }
            }
        }

        result = SchemaUtils.flatten_properties(properties)

        test_name = "nested_type_fields"

        if GENERATE_BASELINE:
            save_baseline(test_name, result, "json")
            pytest.skip("Baseline generated - rerun to validate")

        baseline = load_baseline(test_name, "json")
        if baseline is None:
            pytest.skip(f"No baseline found for {test_name}. Run with GENERATE_BASELINE=true")

        assert result == baseline, f"nested_type_fields output changed!"

    def test_copy_to_and_enabled_fields(self):
        """
        Test fields with copy_to and enabled: false.

        ```
        "first_name": {
          "type": "text",
          "copy_to": "full_name"
        },
        "raw_data": {
          "type": "object",
          "enabled": false
        }
        ```
        """
        from dfe_engine.schema.schema_util import SchemaUtils

        properties = {
            "first_name": {
                "type": "text",
                "copy_to": "full_name"
            },
            "last_name": {
                "type": "text",
                "copy_to": "full_name"
            },
            "full_name": {
                "type": "text"
            },
            "raw_data": {
                "type": "object",
                "enabled": False
            }
        }

        result = SchemaUtils.flatten_properties(properties)

        test_name = "copy_to_and_enabled_fields"

        if GENERATE_BASELINE:
            save_baseline(test_name, result, "json")
            pytest.skip("Baseline generated - rerun to validate")

        baseline = load_baseline(test_name, "json")
        if baseline is None:
            pytest.skip(f"No baseline found for {test_name}. Run with GENERATE_BASELINE=true")

        assert result == baseline, f"copy_to_and_enabled_fields output changed!"

    def test_alias_and_flattened_types(self):
        """
        Test alias and flattened field types.

        ```
        "route_length_miles": {
          "type": "alias",
          "path": "distance"
        },
        "labels": {
          "type": "flattened"
        }
        ```
        """
        from dfe_engine.schema.schema_util import SchemaUtils

        properties = {
            "distance": {
                "type": "float"
            },
            "route_length_miles": {
                "type": "alias",
                "path": "distance"
            },
            "labels": {
                "type": "flattened"
            }
        }

        result = SchemaUtils.flatten_properties(properties)

        test_name = "alias_and_flattened_types"

        if GENERATE_BASELINE:
            save_baseline(test_name, result, "json")
            pytest.skip("Baseline generated - rerun to validate")

        baseline = load_baseline(test_name, "json")
        if baseline is None:
            pytest.skip(f"No baseline found for {test_name}. Run with GENERATE_BASELINE=true")

        assert result == baseline, f"alias_and_flattened_types output changed!"

    def test_complex_combined_structure(self):
        """
        Test a complex structure combining multiple edge cases.

        This simulates a real-world template with:
        - Deeply nested properties
        - Multi-fields
        - Nested type with properties
        - fields with properties (OpenSearch pattern)
        """
        from dfe_engine.schema.schema_util import SchemaUtils

        properties = {
            "user": {
                "properties": {
                    "name": {
                        "type": "text",
                        "fields": {
                            "keyword": {"type": "keyword"}
                        }
                    },
                    "email": {
                        "type": "keyword"
                    },
                    "roles": {
                        "type": "nested",
                        "properties": {
                            "name": {"type": "keyword"},
                            "permissions": {
                                "type": "nested",
                                "properties": {
                                    "action": {"type": "keyword"},
                                    "resource": {"type": "keyword"}
                                }
                            }
                        }
                    }
                }
            },
            "event": {
                "properties": {
                    "message": {
                        "properties": {
                            "fields": {
                                "properties": {
                                    "text": {"type": "keyword"}
                                }
                            }
                        }
                    },
                    "category": {
                        "type": "keyword"
                    }
                }
            }
        }

        result = SchemaUtils.flatten_properties(properties)

        test_name = "complex_combined_structure"

        if GENERATE_BASELINE:
            save_baseline(test_name, result, "json")
            pytest.skip("Baseline generated - rerun to validate")

        baseline = load_baseline(test_name, "json")
        if baseline is None:
            pytest.skip(f"No baseline found for {test_name}. Run with GENERATE_BASELINE=true")

        assert result == baseline, f"complex_combined_structure output changed!"


class TestRealWorldNastyEdgeCases:
    """
    20 Nasty real-world edge cases from ECS, Filebeat, Winlogbeat, and OpenSearch templates.

    These are production patterns that commonly break parsers:
    1. Dense vectors (ML/kNN)
    2. Runtime fields
    3. Join parent-child
    4. Geo shapes and points
    5. Completion suggester
    6. Token count
    7. IP range fields
    8. Histogram
    9. Rank features
    10. Percolator
    11. ECS threat intel nested enrichments
    12. ECS process hierarchy (parent/group_leader/session_leader)
    13. Binary analysis (ELF/PE/Mach-O sections)
    14. Code signature with nested properties
    15. Dynamic templates mixed with explicit mappings
    16. Deeply nested with multi-fields at every level
    17. Object with subobjects: false
    18. Match_only_text type
    19. Wildcard type
    20. Scaled float with scaling factor
    """

    def test_dense_vector_knn_search(self):
        """
        Dense vector field for ML embeddings and kNN search.

        Real ECS 9.x uses dense_vector for semantic_text backing fields.
        This is tricky because it has nested element_type, dims, index, similarity.
        """
        from dfe_engine.schema.schema_util import SchemaUtils

        properties = {
            "ml": {
                "properties": {
                    "inference": {
                        "properties": {
                            "predicted_value_embedding": {
                                "type": "dense_vector",
                                "dims": 768,
                                "index": True,
                                "similarity": "cosine",
                                "index_options": {
                                    "type": "hnsw",
                                    "m": 16,
                                    "ef_construction": 100
                                }
                            },
                            "quantized_embedding": {
                                "type": "dense_vector",
                                "dims": 384,
                                "element_type": "byte",
                                "index": True,
                                "similarity": "dot_product"
                            }
                        }
                    }
                }
            }
        }

        result = SchemaUtils.flatten_properties(properties)
        test_name = "dense_vector_knn"

        if GENERATE_BASELINE:
            save_baseline(test_name, result, "json")
            pytest.skip("Baseline generated - rerun to validate")

        baseline = load_baseline(test_name, "json")
        if baseline is None:
            pytest.skip(f"No baseline found for {test_name}. Run with GENERATE_BASELINE=true")

        assert result == baseline, f"{test_name} output changed!"

    def test_runtime_fields(self):
        """
        Runtime fields computed at query time.

        Runtime fields have 'runtime' mapping type with script definition.
        They don't get indexed but should still be tracked.
        """
        from dfe_engine.schema.schema_util import SchemaUtils

        properties = {
            "event": {
                "properties": {
                    "duration_ms": {
                        "type": "long"
                    },
                    "duration_sec": {
                        "type": "runtime",
                        "runtime_type": "double",
                        "script": {
                            "source": "emit(doc['event.duration_ms'].value / 1000.0)"
                        }
                    }
                }
            },
            "day_of_week": {
                "type": "runtime",
                "runtime_type": "keyword",
                "script": {
                    "source": "emit(doc['@timestamp'].value.dayOfWeekEnum.getDisplayName(TextStyle.FULL, Locale.ROOT))"
                }
            }
        }

        result = SchemaUtils.flatten_properties(properties)
        test_name = "runtime_fields"

        if GENERATE_BASELINE:
            save_baseline(test_name, result, "json")
            pytest.skip("Baseline generated - rerun to validate")

        baseline = load_baseline(test_name, "json")
        if baseline is None:
            pytest.skip(f"No baseline found for {test_name}. Run with GENERATE_BASELINE=true")

        assert result == baseline, f"{test_name} output changed!"

    def test_join_parent_child_relations(self):
        """
        Join field for parent-child document relationships.

        This is particularly nasty because it has 'relations' mapping
        that defines the hierarchy, not actual field properties.
        """
        from dfe_engine.schema.schema_util import SchemaUtils

        properties = {
            "my_join_field": {
                "type": "join",
                "relations": {
                    "question": ["answer", "comment"],
                    "answer": "vote"
                }
            },
            "question_text": {
                "type": "text"
            },
            "answer_text": {
                "type": "text"
            }
        }

        result = SchemaUtils.flatten_properties(properties)
        test_name = "join_parent_child"

        if GENERATE_BASELINE:
            save_baseline(test_name, result, "json")
            pytest.skip("Baseline generated - rerun to validate")

        baseline = load_baseline(test_name, "json")
        if baseline is None:
            pytest.skip(f"No baseline found for {test_name}. Run with GENERATE_BASELINE=true")

        assert result == baseline, f"{test_name} output changed!"

    def test_geo_shape_and_geo_point(self):
        """
        Geographic types: geo_point and geo_shape.

        geo_shape can have strategy, orientation, and other parameters.
        geo_point can have ignore_malformed, null_value, etc.
        """
        from dfe_engine.schema.schema_util import SchemaUtils

        properties = {
            "location": {
                "type": "geo_point",
                "ignore_malformed": True,
                "null_value": {"lat": 0, "lon": 0}
            },
            "service_area": {
                "type": "geo_shape",
                "strategy": "recursive",
                "orientation": "counterclockwise",
                "ignore_malformed": True
            },
            "city": {
                "properties": {
                    "center": {
                        "type": "geo_point"
                    },
                    "boundary": {
                        "type": "geo_shape"
                    },
                    "name": {
                        "type": "keyword"
                    }
                }
            }
        }

        result = SchemaUtils.flatten_properties(properties)
        test_name = "geo_shape_geo_point"

        if GENERATE_BASELINE:
            save_baseline(test_name, result, "json")
            pytest.skip("Baseline generated - rerun to validate")

        baseline = load_baseline(test_name, "json")
        if baseline is None:
            pytest.skip(f"No baseline found for {test_name}. Run with GENERATE_BASELINE=true")

        assert result == baseline, f"{test_name} output changed!"

    def test_completion_suggester(self):
        """
        Completion field for autocomplete/suggester.

        Has special 'contexts' parameter for context-aware suggestions.
        """
        from dfe_engine.schema.schema_util import SchemaUtils

        properties = {
            "suggest": {
                "type": "completion",
                "analyzer": "simple",
                "preserve_separators": True,
                "preserve_position_increments": True,
                "max_input_length": 50,
                "contexts": [
                    {
                        "name": "category",
                        "type": "category",
                        "path": "category_field"
                    },
                    {
                        "name": "location",
                        "type": "geo",
                        "path": "pin",
                        "precision": "100m"
                    }
                ]
            },
            "title": {
                "type": "text",
                "fields": {
                    "suggest": {
                        "type": "completion"
                    }
                }
            }
        }

        result = SchemaUtils.flatten_properties(properties)
        test_name = "completion_suggester"

        if GENERATE_BASELINE:
            save_baseline(test_name, result, "json")
            pytest.skip("Baseline generated - rerun to validate")

        baseline = load_baseline(test_name, "json")
        if baseline is None:
            pytest.skip(f"No baseline found for {test_name}. Run with GENERATE_BASELINE=true")

        assert result == baseline, f"{test_name} output changed!"

    def test_token_count_and_histogram(self):
        """
        token_count for counting tokens, histogram for pre-aggregated data.

        token_count requires 'analyzer' parameter.
        histogram is for pre-bucketed numeric data.
        """
        from dfe_engine.schema.schema_util import SchemaUtils

        properties = {
            "content": {
                "type": "text",
                "fields": {
                    "length": {
                        "type": "token_count",
                        "analyzer": "standard"
                    }
                }
            },
            "latency_histogram": {
                "type": "histogram"
            },
            "metrics": {
                "properties": {
                    "response_time": {
                        "type": "histogram"
                    },
                    "request_count": {
                        "type": "long"
                    }
                }
            }
        }

        result = SchemaUtils.flatten_properties(properties)
        test_name = "token_count_histogram"

        if GENERATE_BASELINE:
            save_baseline(test_name, result, "json")
            pytest.skip("Baseline generated - rerun to validate")

        baseline = load_baseline(test_name, "json")
        if baseline is None:
            pytest.skip(f"No baseline found for {test_name}. Run with GENERATE_BASELINE=true")

        assert result == baseline, f"{test_name} output changed!"

    def test_ip_range_and_date_range(self):
        """
        Range types: ip_range, date_range, integer_range, float_range, long_range, double_range.

        These store ranges rather than single values.
        """
        from dfe_engine.schema.schema_util import SchemaUtils

        properties = {
            "allowed_ip_ranges": {
                "type": "ip_range"
            },
            "expected_delivery": {
                "type": "date_range",
                "format": "yyyy-MM-dd HH:mm:ss||yyyy-MM-dd||epoch_millis"
            },
            "age_bracket": {
                "type": "integer_range"
            },
            "price_range": {
                "type": "float_range",
                "coerce": True
            },
            "network": {
                "properties": {
                    "source_cidr": {
                        "type": "ip_range"
                    },
                    "dest_cidr": {
                        "type": "ip_range"
                    }
                }
            }
        }

        result = SchemaUtils.flatten_properties(properties)
        test_name = "ip_date_range"

        if GENERATE_BASELINE:
            save_baseline(test_name, result, "json")
            pytest.skip("Baseline generated - rerun to validate")

        baseline = load_baseline(test_name, "json")
        if baseline is None:
            pytest.skip(f"No baseline found for {test_name}. Run with GENERATE_BASELINE=true")

        assert result == baseline, f"{test_name} output changed!"

    def test_rank_features_and_rank_feature(self):
        """
        rank_feature and rank_features for relevance boosting.

        rank_features stores a map of feature names to values.
        rank_feature stores a single numeric feature.
        """
        from dfe_engine.schema.schema_util import SchemaUtils

        properties = {
            "pagerank": {
                "type": "rank_feature"
            },
            "url_length": {
                "type": "rank_feature",
                "positive_score_impact": False
            },
            "topics": {
                "type": "rank_features"
            },
            "document": {
                "properties": {
                    "relevance_score": {
                        "type": "rank_feature"
                    },
                    "keyword_features": {
                        "type": "rank_features"
                    }
                }
            }
        }

        result = SchemaUtils.flatten_properties(properties)
        test_name = "rank_features"

        if GENERATE_BASELINE:
            save_baseline(test_name, result, "json")
            pytest.skip("Baseline generated - rerun to validate")

        baseline = load_baseline(test_name, "json")
        if baseline is None:
            pytest.skip(f"No baseline found for {test_name}. Run with GENERATE_BASELINE=true")

        assert result == baseline, f"{test_name} output changed!"

    def test_percolator_type(self):
        """
        Percolator type for storing queries as documents.

        Used for "reverse search" - finding which queries match a document.
        """
        from dfe_engine.schema.schema_util import SchemaUtils

        properties = {
            "query": {
                "type": "percolator"
            },
            "alert_name": {
                "type": "keyword"
            },
            "alert_rules": {
                "properties": {
                    "rule_query": {
                        "type": "percolator"
                    },
                    "rule_name": {
                        "type": "keyword"
                    },
                    "severity": {
                        "type": "keyword"
                    }
                }
            }
        }

        result = SchemaUtils.flatten_properties(properties)
        test_name = "percolator_type"

        if GENERATE_BASELINE:
            save_baseline(test_name, result, "json")
            pytest.skip("Baseline generated - rerun to validate")

        baseline = load_baseline(test_name, "json")
        if baseline is None:
            pytest.skip(f"No baseline found for {test_name}. Run with GENERATE_BASELINE=true")

        assert result == baseline, f"{test_name} output changed!"

    def test_ecs_threat_intel_nested_enrichments(self):
        """
        ECS threat.enrichments structure - deeply nested with nested type.

        This is a real nightmare from ECS 8.x/9.x threat intelligence schema.
        Contains nested indicators, files with hash/signature/binary analysis.
        """
        from dfe_engine.schema.schema_util import SchemaUtils

        properties = {
            "threat": {
                "properties": {
                    "enrichments": {
                        "type": "nested",
                        "properties": {
                            "matched": {
                                "properties": {
                                    "atomic": {"type": "keyword"},
                                    "field": {"type": "keyword"},
                                    "type": {"type": "keyword"}
                                }
                            },
                            "indicator": {
                                "properties": {
                                    "type": {"type": "keyword"},
                                    "ip": {"type": "ip"},
                                    "url": {
                                        "properties": {
                                            "full": {"type": "wildcard"},
                                            "domain": {"type": "keyword"}
                                        }
                                    },
                                    "file": {
                                        "properties": {
                                            "hash": {
                                                "properties": {
                                                    "md5": {"type": "keyword"},
                                                    "sha256": {"type": "keyword"}
                                                }
                                            }
                                        }
                                    },
                                    "first_seen": {"type": "date"},
                                    "last_seen": {"type": "date"}
                                }
                            }
                        }
                    },
                    "indicator": {
                        "properties": {
                            "confidence": {"type": "keyword"},
                            "provider": {"type": "keyword"}
                        }
                    }
                }
            }
        }

        result = SchemaUtils.flatten_properties(properties)
        test_name = "ecs_threat_enrichments"

        if GENERATE_BASELINE:
            save_baseline(test_name, result, "json")
            pytest.skip("Baseline generated - rerun to validate")

        baseline = load_baseline(test_name, "json")
        if baseline is None:
            pytest.skip(f"No baseline found for {test_name}. Run with GENERATE_BASELINE=true")

        assert result == baseline, f"{test_name} output changed!"

    def test_ecs_process_hierarchy(self):
        """
        ECS process hierarchy: parent, group_leader, session_leader, entry_leader.

        Each has recursive structure with user, group, and full process info.
        Real ECS 9.x structure from process.json component template.
        """
        from dfe_engine.schema.schema_util import SchemaUtils

        properties = {
            "process": {
                "properties": {
                    "pid": {"type": "long"},
                    "name": {"type": "keyword"},
                    "executable": {"type": "keyword"},
                    "command_line": {"type": "wildcard"},
                    "hash": {
                        "properties": {
                            "md5": {"type": "keyword"},
                            "sha256": {"type": "keyword"}
                        }
                    },
                    "parent": {
                        "properties": {
                            "pid": {"type": "long"},
                            "name": {"type": "keyword"},
                            "executable": {"type": "keyword"}
                        }
                    },
                    "group_leader": {
                        "properties": {
                            "pid": {"type": "long"},
                            "name": {"type": "keyword"},
                            "user": {
                                "properties": {
                                    "id": {"type": "keyword"},
                                    "name": {"type": "keyword"}
                                }
                            }
                        }
                    },
                    "session_leader": {
                        "properties": {
                            "pid": {"type": "long"},
                            "parent": {
                                "properties": {
                                    "pid": {"type": "long"},
                                    "session_leader": {
                                        "properties": {
                                            "pid": {"type": "long"}
                                        }
                                    }
                                }
                            }
                        }
                    },
                    "entry_leader": {
                        "properties": {
                            "pid": {"type": "long"},
                            "entry_meta": {
                                "properties": {
                                    "source": {
                                        "properties": {
                                            "ip": {"type": "ip"}
                                        }
                                    },
                                    "type": {"type": "keyword"}
                                }
                            }
                        }
                    }
                }
            }
        }

        result = SchemaUtils.flatten_properties(properties)
        test_name = "ecs_process_hierarchy"

        if GENERATE_BASELINE:
            save_baseline(test_name, result, "json")
            pytest.skip("Baseline generated - rerun to validate")

        baseline = load_baseline(test_name, "json")
        if baseline is None:
            pytest.skip(f"No baseline found for {test_name}. Run with GENERATE_BASELINE=true")

        assert result == baseline, f"{test_name} output changed!"

    def test_binary_analysis_elf_pe_macho(self):
        """
        ECS binary analysis: ELF, PE, Mach-O sections.

        Real ECS file.json structure with nested sections (nested type),
        imports, exports, and architecture info.
        """
        from dfe_engine.schema.schema_util import SchemaUtils

        properties = {
            "file": {
                "properties": {
                    "name": {"type": "keyword"},
                    "hash": {
                        "properties": {
                            "md5": {"type": "keyword"},
                            "sha256": {"type": "keyword"}
                        }
                    },
                    "elf": {
                        "properties": {
                            "architecture": {"type": "keyword"},
                            "byte_order": {"type": "keyword"},
                            "cpu_type": {"type": "keyword"},
                            "creation_date": {"type": "date"},
                            "exports": {
                                "type": "flattened"
                            },
                            "imports": {
                                "type": "flattened"
                            },
                            "sections": {
                                "type": "nested",
                                "properties": {
                                    "name": {"type": "keyword"},
                                    "physical_size": {"type": "long"},
                                    "virtual_size": {"type": "long"},
                                    "entropy": {"type": "float"}
                                }
                            },
                            "segments": {
                                "type": "nested",
                                "properties": {
                                    "type": {"type": "keyword"},
                                    "sections": {"type": "keyword"}
                                }
                            }
                        }
                    },
                    "pe": {
                        "properties": {
                            "architecture": {"type": "keyword"},
                            "company": {"type": "keyword"},
                            "imphash": {"type": "keyword"},
                            "sections": {
                                "type": "nested",
                                "properties": {
                                    "name": {"type": "keyword"},
                                    "entropy": {"type": "float"},
                                    "physical_size": {"type": "long"}
                                }
                            }
                        }
                    },
                    "macho": {
                        "properties": {
                            "go_import_hash": {"type": "keyword"},
                            "symhash": {"type": "keyword"},
                            "sections": {
                                "type": "nested",
                                "properties": {
                                    "name": {"type": "keyword"},
                                    "physical_size": {"type": "long"}
                                }
                            }
                        }
                    }
                }
            }
        }

        result = SchemaUtils.flatten_properties(properties)
        test_name = "binary_analysis_elf_pe_macho"

        if GENERATE_BASELINE:
            save_baseline(test_name, result, "json")
            pytest.skip("Baseline generated - rerun to validate")

        baseline = load_baseline(test_name, "json")
        if baseline is None:
            pytest.skip(f"No baseline found for {test_name}. Run with GENERATE_BASELINE=true")

        assert result == baseline, f"{test_name} output changed!"

    def test_code_signature_with_nested(self):
        """
        ECS code_signature with timestamp, status, and nested team info.

        Real-world from ECS file.code_signature and process.code_signature.
        """
        from dfe_engine.schema.schema_util import SchemaUtils

        properties = {
            "process": {
                "properties": {
                    "code_signature": {
                        "properties": {
                            "digest_algorithm": {"type": "keyword"},
                            "exists": {"type": "boolean"},
                            "signing_id": {"type": "keyword"},
                            "status": {"type": "keyword"},
                            "subject_name": {"type": "keyword"},
                            "team_id": {"type": "keyword"},
                            "timestamp": {"type": "date"},
                            "trusted": {"type": "boolean"},
                            "valid": {"type": "boolean"}
                        }
                    },
                    "executable": {"type": "keyword"}
                }
            },
            "dll": {
                "type": "nested",
                "properties": {
                    "name": {"type": "keyword"},
                    "path": {"type": "keyword"},
                    "code_signature": {
                        "properties": {
                            "exists": {"type": "boolean"},
                            "subject_name": {"type": "keyword"},
                            "trusted": {"type": "boolean"}
                        }
                    }
                }
            }
        }

        result = SchemaUtils.flatten_properties(properties)
        test_name = "code_signature_nested"

        if GENERATE_BASELINE:
            save_baseline(test_name, result, "json")
            pytest.skip("Baseline generated - rerun to validate")

        baseline = load_baseline(test_name, "json")
        if baseline is None:
            pytest.skip(f"No baseline found for {test_name}. Run with GENERATE_BASELINE=true")

        assert result == baseline, f"{test_name} output changed!"

    def test_dynamic_templates_with_explicit(self):
        """
        Mix of dynamic_templates and explicit mappings.

        Dynamic templates are often at the root level with explicit properties.
        This tests that we correctly handle both together.
        """
        from dfe_engine.schema.schema_util import SchemaUtils

        # Note: dynamic_templates are usually at mappings level, not properties level
        # This test ensures we handle properties correctly even with extra keys
        properties = {
            "labels": {
                "type": "object",
                "dynamic": True
            },
            "message": {
                "type": "match_only_text"
            },
            "tags": {
                "type": "keyword",
                "ignore_above": 1024
            },
            "metadata": {
                "type": "flattened",
                "depth_limit": 5
            }
        }

        result = SchemaUtils.flatten_properties(properties)
        test_name = "dynamic_templates_explicit"

        if GENERATE_BASELINE:
            save_baseline(test_name, result, "json")
            pytest.skip("Baseline generated - rerun to validate")

        baseline = load_baseline(test_name, "json")
        if baseline is None:
            pytest.skip(f"No baseline found for {test_name}. Run with GENERATE_BASELINE=true")

        assert result == baseline, f"{test_name} output changed!"

    def test_deeply_nested_multifields_every_level(self):
        """
        Multi-fields at every nesting level.

        Real pattern from ECS where text fields have keyword subfields
        throughout the entire hierarchy.
        """
        from dfe_engine.schema.schema_util import SchemaUtils

        properties = {
            "host": {
                "properties": {
                    "name": {
                        "type": "keyword",
                        "fields": {
                            "text": {"type": "match_only_text"}
                        }
                    },
                    "os": {
                        "properties": {
                            "name": {
                                "type": "keyword",
                                "fields": {
                                    "text": {"type": "match_only_text"}
                                }
                            },
                            "full": {
                                "type": "keyword",
                                "fields": {
                                    "text": {"type": "match_only_text"}
                                }
                            },
                            "version": {
                                "type": "keyword"
                            }
                        }
                    },
                    "user": {
                        "properties": {
                            "name": {
                                "type": "keyword",
                                "fields": {
                                    "text": {"type": "match_only_text"}
                                }
                            },
                            "full": {
                                "type": "keyword",
                                "fields": {
                                    "text": {"type": "match_only_text"}
                                }
                            },
                            "domain": {
                                "type": "keyword"
                            }
                        }
                    }
                }
            }
        }

        result = SchemaUtils.flatten_properties(properties)
        test_name = "deeply_nested_multifields"

        if GENERATE_BASELINE:
            save_baseline(test_name, result, "json")
            pytest.skip("Baseline generated - rerun to validate")

        baseline = load_baseline(test_name, "json")
        if baseline is None:
            pytest.skip(f"No baseline found for {test_name}. Run with GENERATE_BASELINE=true")

        assert result == baseline, f"{test_name} output changed!"

    def test_object_with_subobjects_false(self):
        """
        Object with subobjects: false (ES 8.3+).

        This prevents nested objects and flattens all children.
        Commonly used for labels/metrics.
        """
        from dfe_engine.schema.schema_util import SchemaUtils

        properties = {
            "labels": {
                "type": "object",
                "subobjects": False
            },
            "metrics": {
                "type": "object",
                "subobjects": False,
                "properties": {
                    "count": {"type": "long"},
                    "sum": {"type": "double"}
                }
            },
            "container": {
                "properties": {
                    "labels": {
                        "type": "object",
                        "subobjects": False
                    }
                }
            }
        }

        result = SchemaUtils.flatten_properties(properties)
        test_name = "object_subobjects_false"

        if GENERATE_BASELINE:
            save_baseline(test_name, result, "json")
            pytest.skip("Baseline generated - rerun to validate")

        baseline = load_baseline(test_name, "json")
        if baseline is None:
            pytest.skip(f"No baseline found for {test_name}. Run with GENERATE_BASELINE=true")

        assert result == baseline, f"{test_name} output changed!"

    def test_match_only_text_type(self):
        """
        match_only_text type (ES 7.14+).

        Optimized for matching, doesn't support scoring/highlighting.
        Used in ECS 9.x for message field.
        """
        from dfe_engine.schema.schema_util import SchemaUtils

        properties = {
            "message": {
                "type": "match_only_text"
            },
            "error": {
                "properties": {
                    "message": {
                        "type": "match_only_text"
                    },
                    "stack_trace": {
                        "type": "wildcard",
                        "fields": {
                            "text": {"type": "match_only_text"}
                        }
                    }
                }
            },
            "log": {
                "properties": {
                    "original": {
                        "type": "match_only_text",
                        "index": False
                    }
                }
            }
        }

        result = SchemaUtils.flatten_properties(properties)
        test_name = "match_only_text"

        if GENERATE_BASELINE:
            save_baseline(test_name, result, "json")
            pytest.skip("Baseline generated - rerun to validate")

        baseline = load_baseline(test_name, "json")
        if baseline is None:
            pytest.skip(f"No baseline found for {test_name}. Run with GENERATE_BASELINE=true")

        assert result == baseline, f"{test_name} output changed!"

    def test_wildcard_type(self):
        """
        Wildcard type for grep-like pattern matching.

        Used in ECS for URLs, file paths, command lines.
        """
        from dfe_engine.schema.schema_util import SchemaUtils

        properties = {
            "url": {
                "properties": {
                    "full": {
                        "type": "wildcard",
                        "fields": {
                            "text": {"type": "match_only_text"}
                        }
                    },
                    "path": {
                        "type": "wildcard"
                    },
                    "query": {
                        "type": "keyword"
                    }
                }
            },
            "file": {
                "properties": {
                    "path": {
                        "type": "wildcard",
                        "fields": {
                            "text": {"type": "match_only_text"}
                        }
                    },
                    "target_path": {
                        "type": "wildcard"
                    }
                }
            },
            "registry": {
                "properties": {
                    "path": {
                        "type": "wildcard"
                    },
                    "value": {
                        "type": "keyword"
                    }
                }
            }
        }

        result = SchemaUtils.flatten_properties(properties)
        test_name = "wildcard_type"

        if GENERATE_BASELINE:
            save_baseline(test_name, result, "json")
            pytest.skip("Baseline generated - rerun to validate")

        baseline = load_baseline(test_name, "json")
        if baseline is None:
            pytest.skip(f"No baseline found for {test_name}. Run with GENERATE_BASELINE=true")

        assert result == baseline, f"{test_name} output changed!"

    def test_scaled_float_with_scaling_factor(self):
        """
        Scaled float with scaling_factor parameter.

        Used for metrics, percentages, and prices.
        """
        from dfe_engine.schema.schema_util import SchemaUtils

        properties = {
            "host": {
                "properties": {
                    "cpu": {
                        "properties": {
                            "usage": {
                                "type": "scaled_float",
                                "scaling_factor": 1000
                            }
                        }
                    },
                    "disk": {
                        "properties": {
                            "used": {
                                "properties": {
                                    "pct": {
                                        "type": "scaled_float",
                                        "scaling_factor": 1000
                                    }
                                }
                            }
                        }
                    },
                    "memory": {
                        "properties": {
                            "used": {
                                "properties": {
                                    "pct": {
                                        "type": "scaled_float",
                                        "scaling_factor": 1000
                                    },
                                    "bytes": {"type": "long"}
                                }
                            }
                        }
                    }
                }
            },
            "transaction": {
                "properties": {
                    "price": {
                        "type": "scaled_float",
                        "scaling_factor": 100
                    },
                    "discount_pct": {
                        "type": "scaled_float",
                        "scaling_factor": 10000
                    }
                }
            }
        }

        result = SchemaUtils.flatten_properties(properties)
        test_name = "scaled_float"

        if GENERATE_BASELINE:
            save_baseline(test_name, result, "json")
            pytest.skip("Baseline generated - rerun to validate")

        baseline = load_baseline(test_name, "json")
        if baseline is None:
            pytest.skip(f"No baseline found for {test_name}. Run with GENERATE_BASELINE=true")

        assert result == baseline, f"{test_name} output changed!"

    def test_winlogbeat_event_data_dynamic(self):
        """
        Winlogbeat winlog.event_data structure.

        This is dynamic and contains Windows event-specific fields
        that vary by event code (5152, 5156, 4624, etc.)
        """
        from dfe_engine.schema.schema_util import SchemaUtils

        properties = {
            "winlog": {
                "properties": {
                    "event_id": {"type": "keyword"},
                    "channel": {"type": "keyword"},
                    "provider_name": {"type": "keyword"},
                    "event_data": {
                        "properties": {
                            "SubjectUserName": {"type": "keyword"},
                            "SubjectDomainName": {"type": "keyword"},
                            "SubjectUserSid": {"type": "keyword"},
                            "TargetUserName": {"type": "keyword"},
                            "TargetDomainName": {"type": "keyword"},
                            "LogonType": {"type": "keyword"},
                            "IpAddress": {"type": "ip"},
                            "IpPort": {"type": "long"},
                            "ProcessName": {
                                "type": "keyword",
                                "fields": {
                                    "text": {"type": "text"}
                                }
                            },
                            "ProcessId": {"type": "long"},
                            "CommandLine": {
                                "type": "wildcard",
                                "fields": {
                                    "text": {"type": "text"}
                                }
                            }
                        }
                    },
                    "user_data": {
                        "type": "flattened"
                    }
                }
            }
        }

        result = SchemaUtils.flatten_properties(properties)
        test_name = "winlogbeat_event_data"

        if GENERATE_BASELINE:
            save_baseline(test_name, result, "json")
            pytest.skip("Baseline generated - rerun to validate")

        baseline = load_baseline(test_name, "json")
        if baseline is None:
            pytest.skip(f"No baseline found for {test_name}. Run with GENERATE_BASELINE=true")

        assert result == baseline, f"{test_name} output changed!"


class TestRealWorldECSTemplates:
    """
    Tests against REAL ECS 9.x production templates from github.com/elastic/ecs.

    These are the actual generated Elasticsearch component templates used in
    production by Elastic Security, Filebeat, Winlogbeat, and other Elastic products.

    Source: https://raw.githubusercontent.com/elastic/ecs/main/generated/elasticsearch/composable/component/
    """

    TEMPLATES_DIR = Path(__file__).parent / "real_world_templates"

    def _load_ecs_template(self, name: str) -> dict:
        """Load an ECS component template JSON file."""
        filepath = self.TEMPLATES_DIR / f"ecs_{name}.json"
        with open(filepath) as f:
            return json.load(f)

    def _extract_properties(self, template: dict) -> dict:
        """Extract the properties from an ECS component template."""
        return template.get("template", {}).get("mappings", {}).get("properties", {})

    def test_ecs_process_real_template(self):
        """
        Test ECS process.json - 1915 lines, deeply nested.

        Contains: parent, group_leader, session_leader, entry_leader hierarchies,
        code_signature, hash, elf/pe/macho binary analysis, thread capabilities.
        """
        from dfe_engine.schema.schema_util import SchemaUtils

        template = self._load_ecs_template("process")
        properties = self._extract_properties(template)

        result = SchemaUtils.flatten_properties(properties)
        test_name = "ecs_process_real"

        if GENERATE_BASELINE:
            save_baseline(test_name, result, "json")
            pytest.skip("Baseline generated - rerun to validate")

        baseline = load_baseline(test_name, "json")
        if baseline is None:
            pytest.skip(f"No baseline found for {test_name}. Run with GENERATE_BASELINE=true")

        assert result == baseline, f"{test_name} output changed!"

    def test_ecs_threat_real_template(self):
        """
        Test ECS threat.json - 1921 lines, contains nested enrichments.

        Contains: threat.enrichments (nested type), threat.indicator with file/hash/url,
        threat.software, threat.group, threat.tactic, threat.technique hierarchies.
        """
        from dfe_engine.schema.schema_util import SchemaUtils

        template = self._load_ecs_template("threat")
        properties = self._extract_properties(template)

        result = SchemaUtils.flatten_properties(properties)
        test_name = "ecs_threat_real"

        if GENERATE_BASELINE:
            save_baseline(test_name, result, "json")
            pytest.skip("Baseline generated - rerun to validate")

        baseline = load_baseline(test_name, "json")
        if baseline is None:
            pytest.skip(f"No baseline found for {test_name}. Run with GENERATE_BASELINE=true")

        assert result == baseline, f"{test_name} output changed!"

    def test_ecs_file_real_template(self):
        """
        Test ECS file.json - 594 lines, binary analysis structures.

        Contains: file.hash, file.code_signature, file.elf (nested sections/segments),
        file.pe (nested sections), file.macho (nested sections).
        """
        from dfe_engine.schema.schema_util import SchemaUtils

        template = self._load_ecs_template("file")
        properties = self._extract_properties(template)

        result = SchemaUtils.flatten_properties(properties)
        test_name = "ecs_file_real"

        if GENERATE_BASELINE:
            save_baseline(test_name, result, "json")
            pytest.skip("Baseline generated - rerun to validate")

        baseline = load_baseline(test_name, "json")
        if baseline is None:
            pytest.skip(f"No baseline found for {test_name}. Run with GENERATE_BASELINE=true")

        assert result == baseline, f"{test_name} output changed!"

    def test_ecs_host_real_template(self):
        """
        Test ECS host.json - real host template with entity, geo, risk fields.

        Contains: host.entity with multi-fields (text subfield), host.geo with geo_point,
        host.cpu/disk/memory metrics with scaled_float, host.risk with nested scores.
        """
        from dfe_engine.schema.schema_util import SchemaUtils

        template = self._load_ecs_template("host")
        properties = self._extract_properties(template)

        result = SchemaUtils.flatten_properties(properties)
        test_name = "ecs_host_real"

        if GENERATE_BASELINE:
            save_baseline(test_name, result, "json")
            pytest.skip("Baseline generated - rerun to validate")

        baseline = load_baseline(test_name, "json")
        if baseline is None:
            pytest.skip(f"No baseline found for {test_name}. Run with GENERATE_BASELINE=true")

        assert result == baseline, f"{test_name} output changed!"

    def test_ecs_observer_real_template(self):
        """
        Test ECS observer.json - observer (firewall/IDS) template.

        Contains: observer.os.name with 'fields' containing 'properties' pattern,
        observer.geo, observer.ingress/egress network info.
        """
        from dfe_engine.schema.schema_util import SchemaUtils

        template = self._load_ecs_template("observer")
        properties = self._extract_properties(template)

        result = SchemaUtils.flatten_properties(properties)
        test_name = "ecs_observer_real"

        if GENERATE_BASELINE:
            save_baseline(test_name, result, "json")
            pytest.skip("Baseline generated - rerun to validate")

        baseline = load_baseline(test_name, "json")
        if baseline is None:
            pytest.skip(f"No baseline found for {test_name}. Run with GENERATE_BASELINE=true")

        assert result == baseline, f"{test_name} output changed!"

    def test_ecs_event_real_template(self):
        """
        Test ECS event.json - base event fields.

        Contains: event.action, event.category, event.outcome, event.duration,
        event.original, event.reason, event.severity_label.
        """
        from dfe_engine.schema.schema_util import SchemaUtils

        template = self._load_ecs_template("event")
        properties = self._extract_properties(template)

        result = SchemaUtils.flatten_properties(properties)
        test_name = "ecs_event_real"

        if GENERATE_BASELINE:
            save_baseline(test_name, result, "json")
            pytest.skip("Baseline generated - rerun to validate")

        baseline = load_baseline(test_name, "json")
        if baseline is None:
            pytest.skip(f"No baseline found for {test_name}. Run with GENERATE_BASELINE=true")

        assert result == baseline, f"{test_name} output changed!"

    def test_field_count_ecs_process(self):
        """
        Validate we extract a reasonable number of fields from ECS process.

        ECS process.json should have 400+ flattened fields due to the
        recursive parent/group_leader/session_leader/entry_leader structure.
        """
        from dfe_engine.schema.schema_util import SchemaUtils

        template = self._load_ecs_template("process")
        properties = self._extract_properties(template)

        result = SchemaUtils.flatten_properties(properties)

        # ECS process.json is massive - should have many fields
        field_count = len(result)
        assert field_count >= 100, f"Expected at least 100 fields from ECS process, got {field_count}"

        # Verify some known deep fields exist
        expected_fields = [
            "process.pid",
            "process.name",
            "process.parent.pid",
            "process.hash.sha256",
            "process.code_signature.subject_name",
        ]
        for field in expected_fields:
            assert field in result, f"Expected field '{field}' not found in flattened output"

    def test_field_count_ecs_threat(self):
        """
        Validate we extract fields from ECS threat.json correctly.

        ECS threat.json has nested enrichments which should be traversed.
        """
        from dfe_engine.schema.schema_util import SchemaUtils

        template = self._load_ecs_template("threat")
        properties = self._extract_properties(template)

        result = SchemaUtils.flatten_properties(properties)

        # Threat should have the enrichments nested structure
        field_count = len(result)
        assert field_count >= 50, f"Expected at least 50 fields from ECS threat, got {field_count}"

        # Verify nested enrichments fields exist
        expected_fields = [
            "threat.indicator.confidence",
            "threat.indicator.ip",
        ]
        for field in expected_fields:
            assert field in result, f"Expected field '{field}' not found in flattened output"


if __name__ == "__main__":
    import sys
    if "--generate-baseline" in sys.argv:
        os.environ["GENERATE_BASELINE"] = "true"
    pytest.main([__file__, "-v"])
