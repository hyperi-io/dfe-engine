"""
Test suite for default column support across common headers, meta schemas, and derived schemas.

This test suite verifies that:
1. The default column is properly preserved when loading meta schemas and derived schemas
2. Backward compatibility works for schemas without the default column
3. Default values are correctly used through the merge process
4. Derived schemas and additional fields preserve defaults during merging
"""

import logging
import pandas as pd
from dfe_engine.schema.schema_util import SchemaUtils

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
handler = logging.StreamHandler()
handler.setLevel(logging.DEBUG)
logger.addHandler(handler)


# ============================================================================
# TEST 1: Default Column Preservation Through Merge Operations
# ============================================================================


class TestDefaultColumnPreservationInMergeOperations:
    """Tests for default column preservation through merge operations."""

    def test_apply_derived_schema_preserves_defaults(self):
        """Test that apply_derived_schema preserves default column."""
        meta_schema_df = pd.DataFrame(
            {
                "column": ["field1", "field2", "field3"],
                "type": ["String", "Int32", "Float64"],
                "default": ["'unknown'", "0", "0.0"],
                "index_order": [1, 2, 3],
            }
        )

        derived_schema_df = pd.DataFrame(
            {
                "column": ["field1", "field2"],
            }
        )

        result_df = SchemaUtils.apply_derived_schema(
            meta_schema_df=meta_schema_df,
            derived_schema_df=derived_schema_df,
            logger=logger,
        )

        # Verify default column is preserved
        assert "default" in result_df.columns, "default column should be preserved"
        assert len(result_df) == 2, "Should have 2 rows (field1, field2)"
        assert result_df.loc[result_df["column"] == "field1", "default"].values[0] == "'unknown'"
        assert result_df.loc[result_df["column"] == "field2", "default"].values[0] == "0"

    def test_apply_derived_schema_with_wildcards_preserves_defaults(self):
        """Test that wildcard filtering in derived schema preserves defaults."""
        meta_schema_df = pd.DataFrame(
            {
                "column": ["parent", "parent.child1", "parent.child2", "sibling"],
                "type": ["Tuple(String, String)", "String", "String", "String"],
                "default": ["", "'default1'", "'default2'", "'sib_default'"],
                "index_order": [1, 2, 3, 4],
            }
        )

        derived_schema_df = pd.DataFrame(
            {
                "column": ["parent.*", "sibling"],
            }
        )

        result_df = SchemaUtils.apply_derived_schema(
            meta_schema_df=meta_schema_df,
            derived_schema_df=derived_schema_df,
            logger=logger,
        )

        # Verify default column is preserved
        assert "default" in result_df.columns, "default column should be preserved"
        # Check specific defaults are maintained
        for _idx, row in result_df.iterrows():
            if row["column"] == "parent.child1":
                assert row["default"] == "'default1'"
            elif row["column"] == "parent.child2":
                assert row["default"] == "'default2'"
            elif row["column"] == "sibling":
                assert row["default"] == "'sib_default'"

    def test_apply_derived_schema_with_index_order_override(self):
        """Test that derived schema index_order override preserves defaults."""
        meta_schema_df = pd.DataFrame(
            {
                "column": ["field1", "field2", "field3"],
                "type": ["String", "Int32", "Float64"],
                "default": ["'unknown'", "0", "0.0"],
                "index_order": [1, 2, 3],
                "index_type": ["minmax", "minmax", "minmax"],
            }
        )

        derived_schema_df = pd.DataFrame(
            {
                "column": ["field1", "field2", "field3"],
                "index_order": [3, 1, 2],  # Different order
                "index_type": ["minmax", "minmax", "minmax"],
            }
        )

        result_df = SchemaUtils.apply_derived_schema(
            meta_schema_df=meta_schema_df,
            derived_schema_df=derived_schema_df,
            logger=logger,
        )

        # Verify default column is preserved despite index_order change
        assert "default" in result_df.columns, "default column should be preserved"
        assert len(result_df) == 3
        # Verify defaults are still there
        assert result_df.loc[result_df["column"] == "field1", "default"].values[0] == "'unknown'"
        assert result_df.loc[result_df["column"] == "field2", "default"].values[0] == "0"
        assert result_df.loc[result_df["column"] == "field3", "default"].values[0] == "0.0"

    def test_apply_additional_fields_preserves_defaults(self):
        """Test that apply_additional_fields preserves default column."""
        meta_schema_df = pd.DataFrame(
            {
                "column": ["field1", "field2"],
                "type": ["String", "Int32"],
                "default": ["'meta_default'", "0"],
                "index_order": [1, 2],
            }
        )

        additional_fields_df = pd.DataFrame(
            {
                "column": ["field3", "field4"],
                "type": ["Float64", "String"],
                "default": ["1.5", "'additional_default'"],
                "index_order": [3, 4],
            }
        )

        result_df = SchemaUtils.apply_additional_fields(
            meta_schema_df=meta_schema_df,
            additional_schema_df=additional_fields_df,
            logger=logger,
        )

        # Verify default column is preserved
        assert "default" in result_df.columns, "default column should be preserved"
        assert len(result_df) == 4, "Should have 4 rows"

        # Verify all defaults are present
        assert "'meta_default'" in result_df["default"].values
        assert "0" in result_df["default"].values
        assert "1.5" in result_df["default"].values
        assert "'additional_default'" in result_df["default"].values


# ============================================================================
# TEST 2: Default Column in Load Operations
# ============================================================================


class TestDefaultColumnInLoadOperations:
    """Tests for default column loading from various sources."""

    def test_load_schema_from_directory_adds_default_if_missing(self, tmp_path):
        """Test that load_schema_from_directory adds default column if missing.

        This tests BACKWARD COMPATIBILITY - if a meta schema doesn't have the
        default column yet, it should be automatically added to prevent errors.
        """
        schema_df = pd.DataFrame(
            {
                "column": ["field1", "field2"],
                "type": ["String", "Int32"],
                "index_order": [1, 2],
            }
        )

        schema_path = tmp_path / "schema.csv"
        schema_df.to_csv(schema_path, index=False)

        loaded_df = SchemaUtils.load_schema_from_directory(schema_file_path=str(schema_path))

        # Verify default column was added
        assert "default" in loaded_df.columns, (
            "default column should be added (backward compatibility)"
        )
        assert len(loaded_df) == 2

    def test_load_schema_from_directory_preserves_existing_defaults(self, tmp_path):
        """Test that load_schema_from_directory preserves existing default values.

        When a meta schema already has the default column with values, they should
        be preserved exactly as they are in the CSV.
        """
        schema_df = pd.DataFrame(
            {
                "column": ["field1", "field2"],
                "type": ["String", "Int32"],
                "default": ["'preserved'", "100"],
                "index_order": [1, 2],
            }
        )

        schema_path = tmp_path / "schema.csv"
        schema_df.to_csv(schema_path, index=False)

        loaded_df = SchemaUtils.load_schema_from_directory(schema_file_path=str(schema_path))

        # Verify defaults were preserved
        assert loaded_df.loc[loaded_df["column"] == "field1", "default"].values[0] == "'preserved'"
        assert loaded_df.loc[loaded_df["column"] == "field2", "default"].values[0] == "100"


# ============================================================================
# TEST 3: Default Column Backward Compatibility
# ============================================================================


class TestDefaultColumnBackwardCompatibility:
    """Tests for backward compatibility with schemas missing the default column."""

    def test_merge_with_no_default_column_in_derived_schema(self):
        """Test merging when derived schema doesn't have default column.

        If derived schema is old and doesn't have the default column, the merge
        should still work and preserve defaults from meta schema.
        """
        meta_schema_df = pd.DataFrame(
            {
                "column": ["field1", "field2", "field3"],
                "type": ["String", "Int32", "Float64"],
                "default": ["'meta_val'", "10", "1.5"],
                "index_order": [1, 2, 3],
            }
        )

        # Old derived schema format without default column
        derived_schema_df = pd.DataFrame(
            {
                "column": ["field1", "field2"],
            }
        )

        result_df = SchemaUtils.apply_derived_schema(
            meta_schema_df=meta_schema_df,
            derived_schema_df=derived_schema_df,
            logger=logger,
        )

        # Verify defaults from meta schema are still there
        assert "default" in result_df.columns
        assert result_df.loc[result_df["column"] == "field1", "default"].values[0] == "'meta_val'"
        assert result_df.loc[result_df["column"] == "field2", "default"].values[0] == "10"

    def test_merge_with_no_default_column_in_additional_fields(self):
        """Test merging when additional fields don't have default column.

        If additional fields are old format without default column, they should
        be handled gracefully.
        """
        meta_schema_df = pd.DataFrame(
            {
                "column": ["field1"],
                "type": ["String"],
                "default": ["'meta_val'"],
                "index_order": [1],
            }
        )

        # Old additional fields format without default column
        additional_fields_df = pd.DataFrame(
            {
                "column": ["field2"],
                "type": ["Int32"],
                "index_order": [2],
            }
        )

        result_df = SchemaUtils.apply_additional_fields(
            meta_schema_df=meta_schema_df,
            additional_schema_df=additional_fields_df,
            logger=logger,
        )

        # Should still have default column (even if new rows have empty defaults)
        assert "default" in result_df.columns
        assert len(result_df) == 2


# ============================================================================
# TEST 4: Default Column with Partial Values
# ============================================================================


class TestDefaultColumnWithPartialValues:
    """Tests for schemas where only some fields have default values."""

    def test_apply_derived_schema_with_partial_defaults(self):
        """Test derived schema filtering with partial defaults.

        When only some fields in meta schema have defaults, filtering should
        preserve both the fields with defaults and those without.
        """
        meta_schema_df = pd.DataFrame(
            {
                "column": ["field1", "field2", "field3", "field4"],
                "type": ["String", "Int32", "Float64", "DateTime"],
                "default": ["'val1'", "", "'val3'", ""],  # Only field1 and field3 have defaults
                "index_order": [1, 2, 3, 4],
            }
        )

        derived_schema_df = pd.DataFrame(
            {
                "column": ["field1", "field2", "field3", "field4"],
            }
        )

        result_df = SchemaUtils.apply_derived_schema(
            meta_schema_df=meta_schema_df,
            derived_schema_df=derived_schema_df,
            logger=logger,
        )

        # Verify defaults are preserved (with empty strings for fields without defaults)
        assert result_df.loc[result_df["column"] == "field1", "default"].values[0] == "'val1'"
        assert result_df.loc[result_df["column"] == "field2", "default"].values[0] == ""
        assert result_df.loc[result_df["column"] == "field3", "default"].values[0] == "'val3'"
        assert result_df.loc[result_df["column"] == "field4", "default"].values[0] == ""

    def test_apply_additional_fields_with_partial_defaults(self):
        """Test additional fields with partial defaults.

        Additional fields might have some fields with defaults and some without.
        This should be preserved through the merge.
        """
        meta_schema_df = pd.DataFrame(
            {
                "column": ["field1"],
                "type": ["String"],
                "default": ["'existing'"],
                "index_order": [1],
            }
        )

        # Additional fields with mixed defaults
        additional_fields_df = pd.DataFrame(
            {
                "column": ["field2", "field3"],
                "type": ["Int32", "String"],
                "default": ["100", ""],  # field2 has default, field3 doesn't
                "index_order": [2, 3],
            }
        )

        result_df = SchemaUtils.apply_additional_fields(
            meta_schema_df=meta_schema_df,
            additional_schema_df=additional_fields_df,
            logger=logger,
        )

        # Verify all defaults are preserved correctly
        assert result_df.loc[result_df["column"] == "field1", "default"].values[0] == "'existing'"
        assert result_df.loc[result_df["column"] == "field2", "default"].values[0] == "100"
        assert result_df.loc[result_df["column"] == "field3", "default"].values[0] == ""


# ============================================================================
# TEST 5: Schema Merging with Mixed Default Scenarios
# ============================================================================


class TestSchemaMergingWithMixedDefaultScenarios:
    """Tests for complex scenarios combining multiple merge operations."""

    def test_derived_then_additional_fields_preserves_all_defaults(self):
        """Test that chaining derived schema + additional fields preserves defaults.

        When applying both derived schema filtering and then additional fields,
        all default values should be preserved through both operations.
        """
        # Start with meta schema with defaults
        meta_schema_df = pd.DataFrame(
            {
                "column": ["field1", "field2", "field3"],
                "type": ["String", "Int32", "Float64"],
                "default": ["'meta1'", "0", "1.5"],
                "index_order": [1, 2, 3],
            }
        )

        # Apply derived schema (filter to field1, field2)
        derived_schema_df = pd.DataFrame(
            {
                "column": ["field1", "field2"],
            }
        )

        filtered_df = SchemaUtils.apply_derived_schema(
            meta_schema_df=meta_schema_df,
            derived_schema_df=derived_schema_df,
            logger=logger,
        )

        # Then add additional fields
        additional_fields_df = pd.DataFrame(
            {
                "column": ["field4"],
                "type": ["String"],
                "default": ["'additional'"],
                "index_order": [4],
            }
        )

        final_df = SchemaUtils.apply_additional_fields(
            meta_schema_df=filtered_df,
            additional_schema_df=additional_fields_df,
            logger=logger,
        )

        # Verify all defaults are preserved after both operations
        assert "default" in final_df.columns
        assert final_df.loc[final_df["column"] == "field1", "default"].values[0] == "'meta1'"
        assert final_df.loc[final_df["column"] == "field2", "default"].values[0] == "0"
        assert final_df.loc[final_df["column"] == "field4", "default"].values[0] == "'additional'"
        assert len(final_df) == 3

    def test_complex_nested_field_scenario_with_defaults(self):
        """Test complex nested field scenario with partial defaults.

        Test a realistic scenario with nested fields (parent.child) where some
        have defaults and derived schema filters by wildcard.
        """
        meta_schema_df = pd.DataFrame(
            {
                "column": ["parent", "parent.child1", "parent.child2", "parent.child3", "sibling"],
                "type": ["Tuple(String, String, String)", "String", "String", "String", "String"],
                "default": ["", "'default1'", "", "'default3'", "'sibling_default'"],
                "index_order": [1, 2, 3, 4, 5],
            }
        )

        # Use wildcard to include all parent.* and sibling
        derived_schema_df = pd.DataFrame(
            {
                "column": ["parent.*", "sibling"],
            }
        )

        result_df = SchemaUtils.apply_derived_schema(
            meta_schema_df=meta_schema_df,
            derived_schema_df=derived_schema_df,
            logger=logger,
        )

        # Verify defaults are preserved for nested fields
        assert "default" in result_df.columns
        # Check that we have parent.* fields
        assert any(result_df["column"].str.startswith("parent."))
        # Check sibling
        assert "sibling" in result_df["column"].values
        # Verify defaults
        sibling_default = result_df.loc[result_df["column"] == "sibling", "default"].values[0]
        assert sibling_default == "'sibling_default'"
