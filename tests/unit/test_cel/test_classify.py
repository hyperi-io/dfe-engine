#  Project:      dfe-engine
#  File:         tests/unit/test_cel/test_classify.py
#  Purpose:      Tests for CEL expression tier classification
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Tests for dfe_engine.cel.classify.

These MUST match the Rust classifier behaviour in
/projects/hyperi-rustlib/src/transport/filter/classify.rs — if these
tests diverge, the UI validates differently from the runtime engine.
"""

from __future__ import annotations

import pytest

from dfe_engine.cel.classify import (
    FilterTier,
    classify_expression,
)


class TestTier1HasField:
    def test_has_field(self) -> None:
        r = classify_expression("has(_table)")
        assert r.tier == FilterTier.TIER1
        assert r.op is not None
        assert r.op.kind == "field_exists"
        assert r.op.field == "_table"

    def test_not_has_field(self) -> None:
        r = classify_expression("!has(_internal)")
        assert r.tier == FilterTier.TIER1
        assert r.op is not None
        assert r.op.kind == "field_not_exists"
        assert r.op.field == "_internal"

    def test_dotted_path(self) -> None:
        r = classify_expression("has(metadata.source)")
        assert r.tier == FilterTier.TIER1
        assert r.op is not None
        assert r.op.field == "metadata.source"

    def test_whitespace_tolerance(self) -> None:
        r = classify_expression("  has( _table )  ")
        assert r.tier == FilterTier.TIER1


class TestTier1FieldEquals:
    def test_equals(self) -> None:
        r = classify_expression('status == "poison"')
        assert r.tier == FilterTier.TIER1
        assert r.op is not None
        assert r.op.kind == "field_equals"
        assert r.op.field == "status"
        assert r.op.value == "poison"

    def test_not_equals(self) -> None:
        r = classify_expression('source != "internal"')
        assert r.tier == FilterTier.TIER1
        assert r.op is not None
        assert r.op.kind == "field_not_equals"
        assert r.op.field == "source"
        assert r.op.value == "internal"

    def test_nested_equals(self) -> None:
        r = classify_expression('metadata.source == "aws"')
        assert r.tier == FilterTier.TIER1
        assert r.op is not None
        assert r.op.field == "metadata.source"
        assert r.op.value == "aws"


class TestTier1StringOps:
    def test_starts_with(self) -> None:
        r = classify_expression('host.startsWith("prod-")')
        assert r.tier == FilterTier.TIER1
        assert r.op is not None
        assert r.op.kind == "field_starts_with"
        assert r.op.field == "host"
        assert r.op.value == "prod-"

    def test_ends_with(self) -> None:
        r = classify_expression('name.endsWith(".log")')
        assert r.tier == FilterTier.TIER1
        assert r.op is not None
        assert r.op.kind == "field_ends_with"
        assert r.op.value == ".log"

    def test_contains(self) -> None:
        r = classify_expression('path.contains("/api/")')
        assert r.tier == FilterTier.TIER1
        assert r.op is not None
        assert r.op.kind == "field_contains"
        assert r.op.value == "/api/"


class TestTier2:
    def test_compound_and(self) -> None:
        r = classify_expression('severity > 3 && source != "internal"')
        assert r.tier == FilterTier.TIER2
        assert r.fields is not None
        assert "severity" in r.fields
        assert "source" in r.fields

    def test_size_function(self) -> None:
        r = classify_expression("size(items) > 0")
        assert r.tier == FilterTier.TIER2

    def test_field_to_field_comparison(self) -> None:
        r = classify_expression("expected == actual")
        assert r.tier == FilterTier.TIER2

    def test_numeric_comparison(self) -> None:
        r = classify_expression("count >= 100")
        assert r.tier == FilterTier.TIER2


class TestTier3:
    def test_regex_matches(self) -> None:
        r = classify_expression('field.matches("^prod-.*")')
        assert r.tier == FilterTier.TIER3

    def test_iteration_exists(self) -> None:
        r = classify_expression('tags.exists(t, t == "pii")')
        assert r.tier == FilterTier.TIER3

    def test_iteration_map(self) -> None:
        r = classify_expression("items.map(x, x.value)")
        assert r.tier == FilterTier.TIER3

    def test_timestamp(self) -> None:
        r = classify_expression('timestamp("2026-01-01T00:00:00Z") > t')
        assert r.tier == FilterTier.TIER3


class TestExpectedFailures:
    def test_empty_expression(self) -> None:
        with pytest.raises(ValueError):
            classify_expression("")

    def test_whitespace_only(self) -> None:
        with pytest.raises(ValueError):
            classify_expression("   ")


class TestRestrictedFunctionInString:
    def test_matches_in_string_not_detected(self) -> None:
        # "matches" inside a string literal should NOT trigger Tier 3
        r = classify_expression('field == "matches"')
        assert r.tier == FilterTier.TIER1

    def test_exists_in_string_not_detected(self) -> None:
        r = classify_expression('field == "exists"')
        assert r.tier == FilterTier.TIER1
