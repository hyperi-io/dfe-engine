"""Tests for DFE Column Expression Validator + Builder."""

import pytest

from dfe_engine.source.expression import (
    DIRECTIVES,
    ExpressionBuilder,
    ExpressionValidator,
    list_directive_types,
)

# ── Validator: basic parsing ────────────────────────────────────


class TestValidatorBasic:
    """Test basic directive parsing."""

    def test_empty_expression(self):
        result = ExpressionValidator.validate("")
        assert not result.valid
        assert "empty" in result.errors[0].lower()

    def test_whitespace_only(self):
        result = ExpressionValidator.validate("   ")
        assert not result.valid

    def test_no_directive(self):
        result = ExpressionValidator.validate("just a plain string")
        assert not result.valid
        assert "@directive" in result.errors[0]

    def test_unknown_directive(self):
        result = ExpressionValidator.validate("@unknown: something")
        assert not result.valid
        assert "Unknown directive" in result.errors[0]

    def test_all_known_directives(self):
        for d in DIRECTIVES:
            result = ExpressionValidator.validate(f"@{d}: test_value")
            assert result.valid, f"@{d} should be valid"
            assert result.directive == d

    def test_case_insensitive_directive(self):
        result = ExpressionValidator.validate("@SOURCE: timestamp")
        assert result.valid
        assert result.directive == "source"

    def test_leading_whitespace(self):
        result = ExpressionValidator.validate("  @source: timestamp")
        assert result.valid


# ── Validator: @source ──────────────────────────────────────────


class TestValidatorSource:
    """Test @source directive validation."""

    def test_simple_field(self):
        result = ExpressionValidator.validate("@source: org_id")
        assert result.valid
        assert result.directive == "source"
        assert result.field == "org_id"
        assert result.fallback is None
        assert result.candidate_fields == []

    def test_field_with_fallback(self):
        result = ExpressionValidator.validate("@source: timestamp | now()")
        assert result.valid
        assert result.field == "timestamp"
        assert result.fallback == "now()"

    def test_first_syntax(self):
        result = ExpressionValidator.validate("@source: first(tags/_tags/meta/metadata.tags)")
        assert result.valid
        assert result.candidate_fields == ["tags", "_tags", "meta", "metadata.tags"]
        assert result.field == "tags"

    def test_first_single_field(self):
        result = ExpressionValidator.validate("@source: first(timestamp)")
        assert result.valid
        assert result.candidate_fields == ["timestamp"]

    def test_first_empty(self):
        result = ExpressionValidator.validate("@source: first()")
        assert not result.valid
        assert "no candidate" in result.errors[0].lower()

    def test_dotted_field(self):
        result = ExpressionValidator.validate("@source: event.action")
        assert result.valid
        assert result.field == "event.action"

    def test_empty_field(self):
        result = ExpressionValidator.validate("@source: ")
        assert not result.valid

    def test_field_with_complex_fallback(self):
        result = ExpressionValidator.validate("@source: first(timestamp_received/received_at)")
        assert result.valid
        assert result.candidate_fields == ["timestamp_received", "received_at"]

    def test_pipe_with_ch_function_fallback(self):
        result = ExpressionValidator.validate("@source: ts | toDateTime(0)")
        assert result.valid
        assert result.field == "ts"
        assert result.fallback == "toDateTime(0)"


# ── Validator: @generated ───────────────────────────────────────


class TestValidatorGenerated:
    """Test @generated directive validation."""

    def test_simple_expression(self):
        result = ExpressionValidator.validate("@generated: now64(3)")
        assert result.valid
        assert result.directive == "generated"
        assert result.expression == "now64(3)"

    def test_uuid_expression(self):
        result = ExpressionValidator.validate("@generated: generateUUIDv7()")
        assert result.valid
        assert result.expression == "generateUUIDv7()"

    def test_empty_expression(self):
        result = ExpressionValidator.validate("@generated: ")
        assert not result.valid


# ── Validator: @captured ────────────────────────────────────────


class TestValidatorCaptured:
    """Test @captured directive validation."""

    def test_simple_capture(self):
        result = ExpressionValidator.validate("@captured: raw_payload")
        assert result.valid
        assert result.directive == "captured"
        assert result.captured_what == "raw_payload"
        assert result.cast_type is None

    def test_capture_with_cast(self):
        result = ExpressionValidator.validate("@captured: raw_payload as JSON")
        assert result.valid
        assert result.captured_what == "raw_payload"
        assert result.cast_type == "JSON"

    def test_empty_captured(self):
        result = ExpressionValidator.validate("@captured: ")
        assert not result.valid


# ── Validator: @computed ────────────────────────────────────────


class TestValidatorComputed:
    """Test @computed directive validation."""

    def test_geoip_expression(self):
        result = ExpressionValidator.validate("@computed: geoip(client_ip).country")
        assert result.valid
        assert result.directive == "computed"
        assert result.expression == "geoip(client_ip).country"

    def test_empty_computed(self):
        result = ExpressionValidator.validate("@computed: ")
        assert not result.valid


# ── Validator: @config ──────────────────────────────────────────


class TestValidatorConfig:
    """Test @config directive validation."""

    def test_config_path(self):
        result = ExpressionValidator.validate("@config: routing.org_id_field")
        assert result.valid
        assert result.directive == "config"
        assert result.config_path == "routing.org_id_field"

    def test_empty_config(self):
        result = ExpressionValidator.validate("@config: ")
        assert not result.valid


# ── Validator: validate_column_expr ─────────────────────────────


class TestValidateColumnExpr:
    """Test convenience column-level validation."""

    def test_none_expr(self):
        errors = ExpressionValidator.validate_column_expr("_timestamp", None)
        assert errors == []

    def test_valid_expr(self):
        errors = ExpressionValidator.validate_column_expr(
            "_timestamp", "@source: timestamp | now()"
        )
        assert errors == []

    def test_invalid_expr(self):
        errors = ExpressionValidator.validate_column_expr("_bad", "not a directive")
        assert len(errors) == 1
        assert "_bad" in errors[0]


# ── Validator: real-world expressions from dfe-schemas ──────────


class TestRealWorldExpressions:
    """Validate actual expressions used in dfe-schemas YAML files."""

    @pytest.mark.parametrize(
        "expr",
        [
            "@generated: now64(3)",
            "@source: timestamp | now()",
            "@source: first(timestamp_received/received_at)",
            "@generated: generateUUIDv7()",
            "@source: org_id",
            "@source: first(_source)",
            "@captured: raw_payload",
            "@captured: raw_payload as JSON",
            "@source: first(tags/_tags/meta/metadata.tags)",
        ],
    )
    def test_valid_production_expr(self, expr):
        result = ExpressionValidator.validate(expr)
        assert result.valid, f"Expected valid: {expr} — errors: {result.errors}"


# ── Builder ─────────────────────────────────────────────────────


class TestBuilder:
    """Test ExpressionBuilder methods."""

    def test_source_simple(self):
        assert ExpressionBuilder.source("org_id") == "@source: org_id"

    def test_source_with_fallback(self):
        assert (
            ExpressionBuilder.source("timestamp", fallback="now()") == "@source: timestamp | now()"
        )

    def test_source_first(self):
        assert (
            ExpressionBuilder.source_first(["tags", "_tags", "meta"])
            == "@source: first(tags/_tags/meta)"
        )

    def test_generated(self):
        assert ExpressionBuilder.generated("now64(3)") == "@generated: now64(3)"

    def test_captured_simple(self):
        assert ExpressionBuilder.captured("raw_payload") == "@captured: raw_payload"

    def test_captured_with_cast(self):
        assert (
            ExpressionBuilder.captured("raw_payload", cast_type="JSON")
            == "@captured: raw_payload as JSON"
        )

    def test_computed(self):
        assert (
            ExpressionBuilder.computed("geoip(client_ip).country")
            == "@computed: geoip(client_ip).country"
        )

    def test_config(self):
        assert ExpressionBuilder.config("routing.org_id_field") == "@config: routing.org_id_field"


class TestBuilderRoundTrip:
    """Test that built expressions validate correctly."""

    def test_source_roundtrip(self):
        expr = ExpressionBuilder.source("timestamp", fallback="now()")
        result = ExpressionValidator.validate(expr)
        assert result.valid
        assert result.field == "timestamp"
        assert result.fallback == "now()"

    def test_source_first_roundtrip(self):
        expr = ExpressionBuilder.source_first(["a", "b", "c"])
        result = ExpressionValidator.validate(expr)
        assert result.valid
        assert result.candidate_fields == ["a", "b", "c"]

    def test_generated_roundtrip(self):
        expr = ExpressionBuilder.generated("generateUUIDv7()")
        result = ExpressionValidator.validate(expr)
        assert result.valid
        assert result.expression == "generateUUIDv7()"

    def test_captured_roundtrip(self):
        expr = ExpressionBuilder.captured("raw_payload", cast_type="JSON")
        result = ExpressionValidator.validate(expr)
        assert result.valid
        assert result.captured_what == "raw_payload"
        assert result.cast_type == "JSON"


# ── Autocomplete ────────────────────────────────────────────────


class TestAutocomplete:
    """Test autocomplete data helpers."""

    def test_list_directive_types(self):
        types = list_directive_types()
        assert len(types) == 5
        names = {t["name"] for t in types}
        assert names == DIRECTIVES
        for t in types:
            assert "description" in t
            assert "syntax" in t
