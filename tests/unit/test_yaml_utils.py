"""Tests for yaml_utils — deep_merge and YAML operations."""

from __future__ import annotations

import pytest

from dfe_engine import yaml_utils
from dfe_engine.yaml_utils import deep_merge, yaml_dump, yaml_load


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

    def test_list_replace_when_opted_in(self):
        # replace_lists: override list wins wholesale (idempotent re-merge). Nested
        # lists replace too; dicts still merge recursively.
        base = {"items": [1, 2], "keda": {"triggers": [{"a": 1}]}}
        deep_merge(base, {"items": [3, 4], "keda": {"triggers": [{"a": 1}]}}, replace_lists=True)
        assert base["items"] == [3, 4]
        assert base["keda"]["triggers"] == [{"a": 1}]  # not doubled

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


class TestAmbiguousScalarQuoting:
    def test_yaml11_ambiguous_strings_are_quoted_on_dump(self):
        # P2.9: off/on/yes/no/true/false must round-trip as strings through a
        # YAML-1.1 consumer (PyYAML safe_load), so they are quoted on dump.
        import yaml as pyyaml

        from dfe_engine.yaml_utils import yaml_dump_string

        out = yaml_dump_string({"a": "off", "b": "yes", "c": "no", "d": "on", "e": "plain"})
        back = pyyaml.safe_load(out)
        assert back["a"] == "off"  # not coerced to False
        assert back["b"] == "yes"
        assert back["c"] == "no"
        assert back["d"] == "on"
        assert back["e"] == "plain"  # non-ambiguous scalar left unquoted


class TestYamlDumpAtomic:
    def test_roundtrip(self, tmp_path):
        target = tmp_path / "data.yaml"
        yaml_dump({"username": "alice", "enabled": True}, target)
        assert yaml_load(target) == {"username": "alice", "enabled": True}

    def test_interrupted_dump_leaves_original_intact(self, tmp_path, monkeypatch):
        """A crash partway through serialisation must not corrupt the target.

        The atomic write serialises to a temp file then os.replace()s it; if
        the dump raises, os.replace is never reached so the original file is
        byte-for-byte untouched and no partial temp is left behind.
        """
        target = tmp_path / "data.yaml"
        yaml_dump({"username": "alice", "enabled": True}, target)
        original = target.read_text()

        def boom(data, stream):
            stream.write("partial: tru")  # half a document, then crash
            raise RuntimeError("interrupted mid-dump")

        monkeypatch.setattr(yaml_utils._yaml_rt, "dump", boom)
        with pytest.raises(RuntimeError):
            yaml_dump({"username": "bob"}, target)

        assert target.read_text() == original
        leftovers = [p.name for p in tmp_path.iterdir() if p.name != "data.yaml"]
        assert leftovers == []
