"""Tests for yaml_utils — deep_merge and YAML operations."""

from __future__ import annotations

from dfe_engine.yaml_utils import deep_merge


class TestDeepMerge:
    def test_simple_override(self):
        base = {"a": 1}
        result = deep_merge(base, {"a": 2})
        assert result["a"] == 2

    def test_add_new_key(self):
        base = {"a": 1}
        deep_merge(base, {"b": 2})
        assert base == {"a": 1, "b": 2}

    def test_nested_dict_merge(self):
        base = {"a": {"x": 1}}
        deep_merge(base, {"a": {"y": 2}})
        assert base == {"a": {"x": 1, "y": 2}}

    def test_list_append(self):
        base = {"items": [1, 2]}
        deep_merge(base, {"items": [3, 4]})
        assert base["items"] == [1, 2, 3, 4]

    def test_set_union(self):
        base = {"tags": {1, 2}}
        deep_merge(base, {"tags": {2, 3}})
        assert base["tags"] == {1, 2, 3}

    def test_type_mismatch_override(self):
        base = {"a": [1, 2]}
        deep_merge(base, {"a": "replaced"})
        assert base["a"] == "replaced"

    def test_returns_base(self):
        base = {"a": 1}
        result = deep_merge(base, {"b": 2})
        assert result is base

    def test_deeply_nested(self):
        base = {"a": {"b": {"c": 1}}}
        deep_merge(base, {"a": {"b": {"d": 2}}})
        assert base == {"a": {"b": {"c": 1, "d": 2}}}

    def test_empty_override(self):
        base = {"a": 1}
        deep_merge(base, {})
        assert base == {"a": 1}

    def test_empty_base(self):
        base = {}
        deep_merge(base, {"a": 1})
        assert base == {"a": 1}
