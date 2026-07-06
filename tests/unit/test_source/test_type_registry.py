"""Tests for TypeRegistry — primitive-to-ClickHouse type resolution."""

import pytest

from dfe_engine.source.type_registry import (
    InvalidAttributeError,
    InvalidChOverrideError,
    InvalidUseCaseError,
    ResolvedType,
    TypeRegistry,
    UnknownPrimitiveError,
)


@pytest.fixture
def registry() -> TypeRegistry:
    """Load the default type registry."""
    return TypeRegistry.default()


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


class TestTypeRegistryLoading:
    def test_loads_default(self, registry: TypeRegistry):
        assert len(registry.primitives) == 13

    def test_all_primitives_present(self, registry: TypeRegistry):
        expected = {
            "string",
            "text",
            "integer",
            "float",
            "boolean",
            "datetime",
            "timestamp",
            "date",
            "ip",
            "uuid",
            "json",
            "geo_point",
            "enum",
        }
        assert set(registry.primitives) == expected

    def test_all_use_cases_present(self, registry: TypeRegistry):
        expected = {"dimension", "fulltext", "text_search", "range", "bloom"}
        assert set(registry.use_cases) == expected

    def test_all_attributes_present(self, registry: TypeRegistry):
        expected = {"lowcardinality", "nullable", "not_null", "materialized", "alias"}
        assert set(registry.attribute_names) == expected


# ---------------------------------------------------------------------------
# Resolve — normal primitives
# ---------------------------------------------------------------------------


class TestResolveNormal:
    def test_string_default(self, registry: TypeRegistry):
        r = registry.resolve("string")
        assert r == ResolvedType(ch_type="Nullable(String)", codec="ZSTD(1)")

    def test_text_default(self, registry: TypeRegistry):
        r = registry.resolve("text")
        assert r == ResolvedType(ch_type="Nullable(String)", codec="ZSTD(3)")

    def test_integer_default(self, registry: TypeRegistry):
        r = registry.resolve("integer")
        assert r == ResolvedType(ch_type="Nullable(Int64)", codec="ZSTD(1)")

    def test_float_default(self, registry: TypeRegistry):
        r = registry.resolve("float")
        assert r == ResolvedType(ch_type="Nullable(Float64)", codec="ZSTD(1)")

    def test_boolean_not_nullable(self, registry: TypeRegistry):
        r = registry.resolve("boolean")
        assert r == ResolvedType(ch_type="Bool", codec="LZ4")

    def test_datetime_default(self, registry: TypeRegistry):
        r = registry.resolve("datetime")
        assert r == ResolvedType(ch_type="Nullable(DateTime64(3,'UTC'))", codec="Delta, ZSTD(1)")

    def test_timestamp_not_nullable(self, registry: TypeRegistry):
        r = registry.resolve("timestamp")
        assert r == ResolvedType(ch_type="DateTime64(3,'UTC')", codec="Delta, LZ4")

    def test_date_default(self, registry: TypeRegistry):
        r = registry.resolve("date")
        assert r == ResolvedType(ch_type="Nullable(Date)", codec="Delta, ZSTD(1)")

    def test_ip_default(self, registry: TypeRegistry):
        r = registry.resolve("ip")
        assert r == ResolvedType(ch_type="Nullable(IPv6)", codec="LZ4")

    def test_uuid_no_codec(self, registry: TypeRegistry):
        r = registry.resolve("uuid")
        assert r == ResolvedType(ch_type="Nullable(UUID)", codec=None)

    def test_json_default(self, registry: TypeRegistry):
        # JSON is emitted BARE, never Nullable(JSON) - ClickHouse rejects Nullable()
        # around JSON (code 43 on CH 24.8).
        r = registry.resolve("json")
        assert r == ResolvedType(ch_type="JSON", codec="ZSTD(3)")

    def test_geo_point_default(self, registry: TypeRegistry):
        r = registry.resolve("geo_point")
        assert r == ResolvedType(ch_type="Nullable(Point)", codec="ZSTD(1)")

    def test_enum_not_nullable(self, registry: TypeRegistry):
        r = registry.resolve("enum")
        assert r.ch_type == "Enum8(...)"
        assert r.codec == "ZSTD(1)"


# ---------------------------------------------------------------------------
# Resolve — with attributes
# ---------------------------------------------------------------------------


class TestResolveAttributes:
    def test_lowcardinality(self, registry: TypeRegistry):
        r = registry.resolve("string", attributes=["lowcardinality"])
        assert r.ch_type == "LowCardinality(Nullable(String))"

    def test_lowcardinality_not_null(self, registry: TypeRegistry):
        r = registry.resolve("string", attributes=["lowcardinality", "not_null"])
        assert r.ch_type == "LowCardinality(String)"

    def test_nullable_override(self, registry: TypeRegistry):
        """Boolean is not nullable by default, but nullable attribute forces it."""
        r = registry.resolve("boolean", attributes=["nullable"])
        assert r.ch_type == "Nullable(Bool)"

    def test_not_null_override(self, registry: TypeRegistry):
        """String is nullable by default, but not_null forces non-nullable."""
        r = registry.resolve("string", attributes=["not_null"])
        assert r.ch_type == "String"

    def test_lowcardinality_integer(self, registry: TypeRegistry):
        r = registry.resolve("integer", attributes=["lowcardinality"])
        assert r.ch_type == "LowCardinality(Nullable(Int64))"


# ---------------------------------------------------------------------------
# Resolve — with ch_override
# ---------------------------------------------------------------------------


class TestResolveChOverride:
    def test_override_verbatim(self, registry: TypeRegistry):
        r = registry.resolve("integer", ch_override="UInt16")
        assert r.ch_type == "UInt16"
        assert r.codec is None

    def test_override_with_lowcardinality(self, registry: TypeRegistry):
        r = registry.resolve("integer", attributes=["lowcardinality"], ch_override="UInt16")
        assert r.ch_type == "LowCardinality(UInt16)"
        assert r.codec is None

    def test_override_parameterised(self, registry: TypeRegistry):
        r = registry.resolve("datetime", ch_override="DateTime64(6,'UTC')")
        assert r.ch_type == "DateTime64(6,'UTC')"

    def test_override_complex(self, registry: TypeRegistry):
        r = registry.resolve("json", ch_override="Map(String,String)")
        assert r.ch_type == "Map(String,String)"

    def test_override_no_auto_nullable(self, registry: TypeRegistry):
        """ch_override disables automatic Nullable wrapping."""
        r = registry.resolve("string", ch_override="FixedString(16)")
        assert "Nullable" not in r.ch_type
        assert r.ch_type == "FixedString(16)"


# ---------------------------------------------------------------------------
# Resolve — with use_case (validation only)
# ---------------------------------------------------------------------------


class TestResolveUseCase:
    def test_valid_use_case(self, registry: TypeRegistry):
        r = registry.resolve("string", use_case="dimension")
        assert r.ch_type == "Nullable(String)"

    def test_invalid_use_case_rejected(self, registry: TypeRegistry):
        with pytest.raises(InvalidUseCaseError, match=r"fulltext.*not valid.*integer"):
            registry.resolve("integer", use_case="fulltext")


# ---------------------------------------------------------------------------
# Validation — use_case
# ---------------------------------------------------------------------------


class TestValidateUseCase:
    @pytest.mark.parametrize("primitive", ["string", "integer", "boolean", "enum", "ip", "uuid"])
    def test_dimension_valid(self, registry: TypeRegistry, primitive: str):
        registry.validate_use_case(primitive, "dimension")

    @pytest.mark.parametrize(
        "primitive", ["float", "text", "datetime", "date", "json", "geo_point"]
    )
    def test_dimension_invalid(self, registry: TypeRegistry, primitive: str):
        with pytest.raises(InvalidUseCaseError):
            registry.validate_use_case(primitive, "dimension")

    @pytest.mark.parametrize("primitive", ["string", "text"])
    def test_fulltext_valid(self, registry: TypeRegistry, primitive: str):
        registry.validate_use_case(primitive, "fulltext")

    @pytest.mark.parametrize("primitive", ["integer", "float", "boolean"])
    def test_fulltext_invalid(self, registry: TypeRegistry, primitive: str):
        with pytest.raises(InvalidUseCaseError):
            registry.validate_use_case(primitive, "fulltext")

    @pytest.mark.parametrize(
        "primitive", ["integer", "float", "datetime", "timestamp", "date", "ip"]
    )
    def test_range_valid(self, registry: TypeRegistry, primitive: str):
        registry.validate_use_case(primitive, "range")

    @pytest.mark.parametrize("primitive", ["string", "text", "boolean", "json"])
    def test_range_invalid(self, registry: TypeRegistry, primitive: str):
        with pytest.raises(InvalidUseCaseError):
            registry.validate_use_case(primitive, "range")

    @pytest.mark.parametrize("primitive", ["string", "uuid"])
    def test_bloom_valid(self, registry: TypeRegistry, primitive: str):
        registry.validate_use_case(primitive, "bloom")

    def test_unknown_primitive(self, registry: TypeRegistry):
        with pytest.raises(UnknownPrimitiveError):
            registry.validate_use_case("nonexistent", "dimension")

    def test_unknown_use_case(self, registry: TypeRegistry):
        with pytest.raises(InvalidUseCaseError, match="Unknown use case"):
            registry.validate_use_case("string", "nonexistent")


# ---------------------------------------------------------------------------
# Validation — attribute
# ---------------------------------------------------------------------------


class TestValidateAttribute:
    @pytest.mark.parametrize("primitive", ["string", "text", "integer", "float", "date", "ip"])
    def test_lowcardinality_valid(self, registry: TypeRegistry, primitive: str):
        registry.validate_attribute(primitive, "lowcardinality")

    @pytest.mark.parametrize("primitive", ["json", "geo_point", "boolean", "uuid", "enum"])
    def test_lowcardinality_invalid(self, registry: TypeRegistry, primitive: str):
        with pytest.raises(InvalidAttributeError):
            registry.validate_attribute(primitive, "lowcardinality")

    @pytest.mark.parametrize("primitive", ["string", "integer", "boolean", "json"])
    def test_nullable_valid_for_all(self, registry: TypeRegistry, primitive: str):
        registry.validate_attribute(primitive, "nullable")

    @pytest.mark.parametrize("primitive", ["string", "integer", "boolean", "json"])
    def test_materialized_valid_for_all(self, registry: TypeRegistry, primitive: str):
        registry.validate_attribute(primitive, "materialized")

    def test_unknown_attribute(self, registry: TypeRegistry):
        with pytest.raises(InvalidAttributeError, match="Unknown attribute"):
            registry.validate_attribute("string", "nonexistent")

    def test_unknown_primitive(self, registry: TypeRegistry):
        with pytest.raises(UnknownPrimitiveError):
            registry.validate_attribute("nonexistent", "nullable")


# ---------------------------------------------------------------------------
# Validation — ch_override
# ---------------------------------------------------------------------------


class TestValidateChOverride:
    @pytest.mark.parametrize(
        "override",
        [
            "Int8",
            "Int16",
            "Int32",
            "Int64",
            "Int128",
            "Int256",
            "UInt8",
            "UInt16",
            "UInt32",
            "UInt64",
            "Float32",
            "Float64",
            "String",
            "Bool",
            "Date",
            "Date32",
            "DateTime",
            "IPv4",
            "IPv6",
            "UUID",
            "JSON",
            "Point",
            "Ring",
            "Polygon",
            "MultiPolygon",
            "Dynamic",
        ],
    )
    def test_exact_types(self, registry: TypeRegistry, override: str):
        registry.validate_ch_override(override)

    @pytest.mark.parametrize(
        "override",
        [
            "FixedString(16)",
            "DateTime64(3)",
            "DateTime64(6,'UTC')",
            "Decimal(10,2)",
            "Decimal32(4)",
            "Decimal64(8)",
            "Decimal128(18)",
            "Enum8('a'=1,'b'=2)",
            "Enum16('x'=1)",
            "Array(String)",
            "Map(String,Int64)",
            "Tuple(String,Int64,Float64)",
            "Nested(key String, value String)",
            "AggregateFunction(groupBitmap, UInt32)",
            "SimpleAggregateFunction(sum, Int64)",
            "Variant(String, Int64)",
        ],
    )
    def test_parameterised_types(self, registry: TypeRegistry, override: str):
        registry.validate_ch_override(override)

    def test_invalid_type(self, registry: TypeRegistry):
        with pytest.raises(InvalidChOverrideError):
            registry.validate_ch_override("NotAType")

    def test_invalid_type_message(self, registry: TypeRegistry):
        with pytest.raises(InvalidChOverrideError, match="not a supported ClickHouse type"):
            registry.validate_ch_override("Blob")


# ---------------------------------------------------------------------------
# Introspection
# ---------------------------------------------------------------------------


class TestIntrospection:
    def test_valid_primitives_for_use_case(self, registry: TypeRegistry):
        prims = registry.valid_primitives_for_use_case("dimension")
        assert "string" in prims
        assert "integer" in prims
        assert "text" not in prims

    def test_valid_primitives_for_attribute_specific(self, registry: TypeRegistry):
        prims = registry.valid_primitives_for_attribute("lowcardinality")
        assert isinstance(prims, list)
        assert "string" in prims
        assert "json" not in prims

    def test_valid_primitives_for_attribute_all(self, registry: TypeRegistry):
        result = registry.valid_primitives_for_attribute("nullable")
        assert result == "all"

    def test_primitive_defaults(self, registry: TypeRegistry):
        defaults = registry.primitive_defaults("string")
        assert defaults["ch_type"] == "String"
        assert defaults["codec"] == "ZSTD(1)"
        assert defaults["nullable"] is True

    def test_primitive_defaults_unknown(self, registry: TypeRegistry):
        with pytest.raises(UnknownPrimitiveError):
            registry.primitive_defaults("nonexistent")
