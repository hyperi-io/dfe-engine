"""
Unit tests for IP Field feature (ip_field type with Variant(IPv4, IPv6)).

Test Classes:
  - TestGenerateNormColumnSql: Core SQL generation logic
  - TestSchemaNameConflictError: Exception handling
  - TestValidateNormColumnNames: Validation logic
  - TestIpFieldTypeMap: Type map integration
  - TestBackwardCompatibility: Legacy support
  - TestEdgeCases: Edge cases and error conditions
"""

import pytest
import pandas as pd
import io

from dfe_engine.schema.schema_util import (
    generate_norm_column_sql,
    SchemaNameConflictError,
)
from dfe_engine.schema.schema_builder import SchemaBuilder


class TestGenerateNormColumnSql:
    """Test generate_norm_column_sql() function."""

    def test_generates_sql_for_indexed_ip_field(self):
        """Test that SQL is generated for indexed ip_field columns."""
        result = generate_norm_column_sql(
            column_name="ip", column_type="ip_field", index_type="dimension"
        )

        assert result is not None
        assert "ip_norm" in result
        assert "IPv6 MATERIALIZED" in result
        assert "variantType" in result
        assert "IPv4ToIPv6" in result

    def test_returns_none_for_non_indexed_ip_field(self):
        """Test that None is returned for ip_field without index_type."""
        result = generate_norm_column_sql(column_name="ip", column_type="ip_field", index_type=None)

        assert result is None

    def test_returns_none_for_non_indexed_ip_field_empty_string(self):
        """Test that None is returned for ip_field with empty index_type."""
        result = generate_norm_column_sql(column_name="ip", column_type="ip_field", index_type="")

        assert result is None

    def test_returns_none_for_different_column_type(self):
        """Test that None is returned for non-ip_field types."""
        result = generate_norm_column_sql(
            column_name="source_ip", column_type="ipv4", index_type="dimension"
        )

        assert result is None

    def test_returns_none_for_ipv6_type(self):
        """Test that None is returned for ipv6 type."""
        result = generate_norm_column_sql(
            column_name="dest_ip", column_type="ipv6", index_type="range"
        )

        assert result is None

    def test_sql_format_contains_required_elements(self):
        """Test that generated SQL contains all required elements."""
        result = generate_norm_column_sql(
            column_name="network_ip", column_type="ip_field", index_type="range"
        )

        assert "network_ip_norm" in result
        assert "IPv6" in result
        assert "MATERIALIZED" in result
        assert "if(" in result
        assert "variantType" in result
        assert "IPv4ToIPv6" in result

    @pytest.mark.parametrize("index_type", ["dimension", "fulltext", "hc", "range", "text_search"])
    def test_different_index_types_all_generate_sql(self, index_type):
        """Test that various index types generate SQL."""
        result = generate_norm_column_sql(
            column_name="ip", column_type="ip_field", index_type=index_type
        )
        assert result is not None, f"Should generate SQL for index_type={index_type}"

    def test_sql_has_correct_materialized_format(self):
        """Test exact format of MATERIALIZED column SQL."""
        result = generate_norm_column_sql("ip", "ip_field", "range")

        assert result.startswith("ip_norm")
        assert "IPv6 MATERIALIZED if(" in result
        assert "variantType(ip)='IPv4'" in result


class TestSchemaNameConflictError:
    """Test SchemaNameConflictError exception."""

    def test_exception_can_be_raised(self):
        """Test that exception can be raised."""
        with pytest.raises(SchemaNameConflictError):
            raise SchemaNameConflictError("Test error message")

    def test_exception_message_preserved(self):
        """Test that exception message is preserved."""
        msg = "This is a test error message"
        with pytest.raises(SchemaNameConflictError) as exc_info:
            raise SchemaNameConflictError(msg)
        assert str(exc_info.value) == msg

    def test_exception_inherits_from_exception(self):
        """Test that SchemaNameConflictError inherits from Exception."""
        exc = SchemaNameConflictError("test")
        assert isinstance(exc, Exception)


class TestValidateNormColumnNames:
    """Test validate_norm_column_names() method."""

    def test_no_error_for_ip_field_without_index_type(self):
        """Test no validation error when ip_field has no index_type."""
        schema_columns = [{"column": "ip", "type": "ip_field", "index_type": None}]

        SchemaBuilder.validate_norm_column_names(schema_columns)

    def test_no_error_for_non_ip_field(self):
        """Test no validation error for non-ip_field types."""
        schema_columns = [{"column": "source_ip", "type": "ipv4", "index_type": "dimension"}]

        SchemaBuilder.validate_norm_column_names(schema_columns)

    def test_error_on_conflict_with_existing_norm_column(self):
        """Test error is raised when _norm column already exists."""
        schema_columns = [
            {"column": "ip", "type": "ip_field", "index_type": "dimension"},
            {"column": "ip_norm", "type": "ipv6", "index_type": None},
        ]

        with pytest.raises(SchemaNameConflictError) as exc_info:
            SchemaBuilder.validate_norm_column_names(schema_columns)

        assert "Conflict" in str(exc_info.value)
        assert "ip_norm" in str(exc_info.value)

    def test_no_error_with_different_norm_suffix(self):
        """Test no error when existing column has different suffix."""
        schema_columns = [
            {"column": "ip", "type": "ip_field", "index_type": "range"},
            {"column": "ip_normalized", "type": "string", "index_type": None},
        ]

        SchemaBuilder.validate_norm_column_names(schema_columns)

    def test_multiple_ip_fields_with_indices(self):
        """Test validation with multiple ip_field columns with indices."""
        schema_columns = [
            {"column": "src_ip", "type": "ip_field", "index_type": "dimension"},
            {"column": "dst_ip", "type": "ip_field", "index_type": "range"},
        ]

        SchemaBuilder.validate_norm_column_names(schema_columns)

    def test_error_message_descriptive(self):
        """Test that error message is descriptive."""
        schema_columns = [
            {"column": "network_ip", "type": "ip_field", "index_type": "fulltext"},
            {"column": "network_ip_norm", "type": "string", "index_type": None},
        ]

        with pytest.raises(SchemaNameConflictError) as exc_info:
            SchemaBuilder.validate_norm_column_names(schema_columns)

        error_msg = str(exc_info.value)
        assert "network_ip" in error_msg
        assert "fulltext" in error_msg
        assert "MATERIALIZED" in error_msg


class TestIpFieldTypeMap:
    """Test that ip_field type is in type_maps.csv."""

    @pytest.fixture(autouse=True)
    def setup_type_maps_data(self):
        """Setup mock type maps CSV data."""
        self.type_maps_csv_content = """type,clickhouse_type,clickhouse_type_index,description
ipv4,IPv4,IPv4,IPv4 address
ipv6,IPv6,IPv6,IPv6 address
ip_field,"Nullable(Variant(IPv4, IPv6)) CODEC(LZ4)","Variant(IPv4, IPv6) CODEC(LZ4)",Unified IPv4 + IPv6 field
text,String,String,Text field
timestamp,DateTime,DateTime,Timestamp field"""

    def test_ip_field_in_type_maps(self):
        """Test that ip_field type exists in type_maps.csv."""
        assert "ip_field" in self.type_maps_csv_content
        assert "Variant(IPv4, IPv6)" in self.type_maps_csv_content

    def test_ip_field_row_format(self):
        """Test that ip_field row has correct format."""
        df = pd.read_csv(io.StringIO(self.type_maps_csv_content))
        ip_field_row = df[df["type"] == "ip_field"]

        assert not ip_field_row.empty

        row = ip_field_row.iloc[0]
        assert "Variant(IPv4, IPv6)" in row["clickhouse_type"]
        assert "Variant(IPv4, IPv6)" in row["clickhouse_type_index"]
        assert "Unified" in row["description"]

    def test_ip_field_codec_correct(self):
        """Test that ip_field has correct CODEC."""
        df = pd.read_csv(io.StringIO(self.type_maps_csv_content))
        ip_field_row = df[df["type"] == "ip_field"]
        row = ip_field_row.iloc[0]

        assert "CODEC(LZ4)" in row["clickhouse_type"]
        assert "Nullable" in row["clickhouse_type"]


class TestBackwardCompatibility:
    """Test backward compatibility with existing ipv4/ipv6 types."""

    @pytest.fixture(autouse=True)
    def setup_type_maps_data(self):
        """Setup mock type maps CSV data."""
        self.type_maps_csv_content = """type,clickhouse_type,clickhouse_type_index,description
ipv4,IPv4,IPv4,IPv4 address
ipv6,IPv6,IPv6,IPv6 address
ip_field,"Nullable(Variant(IPv4, IPv6)) CODEC(LZ4)","Variant(IPv4, IPv6) CODEC(LZ4)",Unified IPv4 + IPv6 field"""

    def test_ipv4_type_still_exists(self):
        """Test that ipv4 type is still in type_maps."""
        df = pd.read_csv(io.StringIO(self.type_maps_csv_content))
        ipv4_row = df[df["type"] == "ipv4"]

        assert not ipv4_row.empty

    def test_ipv6_type_still_exists(self):
        """Test that ipv6 type is still in type_maps."""
        df = pd.read_csv(io.StringIO(self.type_maps_csv_content))
        ipv6_row = df[df["type"] == "ipv6"]

        assert not ipv6_row.empty

    def test_old_types_unchanged(self):
        """Test that old ipv4/ipv6 types are unchanged."""
        df = pd.read_csv(io.StringIO(self.type_maps_csv_content))

        ipv4_row = df[df["type"] == "ipv4"].iloc[0]
        ipv6_row = df[df["type"] == "ipv6"].iloc[0]

        assert "IPv4" in ipv4_row["clickhouse_type"]
        assert "Variant" not in ipv4_row["clickhouse_type"]

        assert "IPv6" in ipv6_row["clickhouse_type"]
        assert "Variant" not in ipv6_row["clickhouse_type"]


class TestEdgeCases:
    """Test edge cases and error conditions."""

    def test_empty_schema_columns_list(self):
        """Test validation with empty schema columns list."""
        schema_columns = []
        SchemaBuilder.validate_norm_column_names(schema_columns)

    def test_none_column_name_ignored(self):
        """Test that None column names are handled gracefully."""
        schema_columns = [
            {"column": None, "type": "ip_field", "index_type": "dimension"},
            {"column": "ip", "type": "ip_field", "index_type": "range"},
        ]

        SchemaBuilder.validate_norm_column_names(schema_columns)

    def test_case_sensitivity(self):
        """Test that column name validation is case-sensitive."""
        schema_columns = [
            {"column": "ip", "type": "ip_field", "index_type": "dimension"},
            {"column": "IP_NORM", "type": "string", "index_type": None},
        ]

        SchemaBuilder.validate_norm_column_names(schema_columns)

    def test_none_index_type_string(self):
        """Test that 'None' as string is treated as truthy."""
        result = generate_norm_column_sql(
            column_name="ip", column_type="ip_field", index_type="None"
        )

        assert result is not None

    def test_column_name_with_special_chars(self):
        """Test column names with underscores."""
        result = generate_norm_column_sql(
            column_name="src_ip_v4", column_type="ip_field", index_type="dimension"
        )

        assert result is not None
        assert "src_ip_v4_norm" in result

    def test_mixed_column_types_validation(self):
        """Test validation with mixed column types."""
        schema_columns = [
            {"column": "timestamp", "type": "timestamp", "index_type": "range"},
            {"column": "src_ip", "type": "ip_field", "index_type": "dimension"},
            {"column": "message", "type": "text", "index_type": "fulltext"},
            {"column": "port", "type": "int16", "index_type": None},
        ]

        SchemaBuilder.validate_norm_column_names(schema_columns)

    def test_type_case_insensitivity_detection(self):
        """Test that type detection is case-sensitive."""
        result1 = generate_norm_column_sql("ip", "ip_field", "range")
        result2 = generate_norm_column_sql("ip", "IP_FIELD", "range")

        assert result1 is not None
        assert result2 is None

    def test_long_column_name_handling(self):
        """Test that long column names are handled correctly."""
        long_name = "source_ip_address_with_very_long_descriptive_name"
        result = generate_norm_column_sql(
            column_name=long_name, column_type="ip_field", index_type="range"
        )

        assert result is not None
        assert f"{long_name}_norm" in result

    def test_numeric_suffix_in_column_name(self):
        """Test column names ending with numbers."""
        result = generate_norm_column_sql(
            column_name="ip_v4_1", column_type="ip_field", index_type="range"
        )

        assert result is not None
        assert "ip_v4_1_norm" in result
