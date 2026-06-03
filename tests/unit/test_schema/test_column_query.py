"""Tests for meta-schema column filter helpers."""

from __future__ import annotations

from dfe_engine.schema.column_query import filter_columns
from dfe_engine.schema.models import SchemaColumn


def _col(**kwargs: object) -> SchemaColumn:
    return SchemaColumn.model_validate({"name": "n", "type": "string", **kwargs})


class TestFilterColumns:
    def test_expr_substring_filter(self):
        cols = [
            _col(name="a", expr="@source: A"),
            _col(name="b"),
        ]
        out = filter_columns(cols, expr="@source")
        assert [c.name for c in out] == ["a"]

    def test_substring_filter(self):
        cols = [_col(name="event_id"), _col(name="user")]
        out = filter_columns(cols, name="event")
        assert len(out) == 1
        assert out[0].name == "event_id"

    def test_search_all_fields(self):
        cols = [_col(name="x", comment="keep"), _col(name="y")]
        out = filter_columns(cols, search="keep")
        assert len(out) == 1
        assert out[0].matched_searchable == ["comment"]

    def test_search_null_matches_nullable_substring(self):
        cols = [
            _col(name="a", attribute=["Nullable"]),
            _col(name="b", comment="not null"),
            _col(name="c"),
        ]
        out = filter_columns(cols, search="null")
        assert {c.name for c in out} == {"a", "b"}

    def test_search_nullable_substring_still_works(self):
        cols = [_col(name="a", attribute=["Nullable"]), _col(name="b")]
        out = filter_columns(cols, search="Nullable")
        assert [c.name for c in out] == ["a"]

    def test_field_filter_null_matches_nullable_substring(self):
        cols = [_col(name="a", attribute=["Nullable"]), _col(name="b")]
        out = filter_columns(cols, attribute="null")
        assert [c.name for c in out] == ["a"]

    def test_attribute_substring_match(self):
        cols = [
            _col(name="a", attribute=["pii"]),
            _col(name="b"),
        ]
        assert filter_columns(cols, attribute="pii")[0].name == "a"

    def test_searchable_columns_limits_search(self):
        cols = [
            _col(name="find_me", comment="noise"),
            _col(name="other", comment="find_me"),
        ]
        out = filter_columns(cols, search="find", searchable_columns=["name"])
        assert len(out) == 1
        assert out[0].name == "find_me"
        assert out[0].matched_searchable == ["name"]

    def test_searchable_columns_type_filter_alias(self):
        cols = [_col(name="a", type="integer"), _col(name="b", type="string")]
        out = filter_columns(cols, search="int", searchable_columns=["type_filter"])
        assert [c.name for c in out] == ["a"]
        assert out[0].matched_searchable == ["type"]

    def test_matched_searchable_empty_without_search(self):
        cols = [_col(name="a")]
        out = filter_columns(cols, name="a")
        assert out[0].matched_searchable == []

    def test_invalid_searchable_columns_raises(self):
        import pytest

        with pytest.raises(ValueError, match="Invalid searchable_columns"):
            filter_columns([_col()], search="x", searchable_columns=["nope"])
