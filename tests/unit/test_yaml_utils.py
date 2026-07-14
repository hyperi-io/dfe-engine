"""Tests for yaml_utils — deep_merge and YAML operations."""

from __future__ import annotations

import threading

from dfe_engine.yaml_utils import deep_merge, yaml_dump, yaml_dump_string, yaml_load


class TestConcurrentYaml:
    """YAML dump/load must be resilient under multi-thread concurrency.

    Regression for the ComposerError ('expected a single document ... but found
    another') that a shared non-thread-safe ruamel instance + non-atomic writes
    produced when the API worker-thread pool and a background task dumped/loaded
    at once. Root cause: yaml_utils.py.
    """

    def test_concurrent_dump_load_same_path_never_corrupts(self, tmp_path):
        path = tmp_path / "shared.yaml"
        # Seed so a reader always has a complete file to read.
        yaml_dump({"members": ["seed"], "n": 0}, path)
        errors: list[BaseException] = []
        stop = threading.Event()

        def writer(worker: int) -> None:
            try:
                for i in range(60):
                    # Varying-length payloads make a truncation-tail race
                    # (a stale second document) more likely if writes are not atomic.
                    yaml_dump({"members": [f"u{worker}-{j}" for j in range(i % 12)], "n": i}, path)
            except BaseException as exc:
                errors.append(exc)
            finally:
                stop.set()

        def reader() -> None:
            try:
                while not stop.is_set():
                    data = yaml_load(path)
                    # A corrupt/doubled file would raise; a partial write would
                    # yield a non-dict. Either is a failure.
                    assert isinstance(data, dict)
                    assert "n" in data
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(w,)) for w in range(4)]
        threads += [threading.Thread(target=reader) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, f"concurrent YAML ops corrupted: {errors[0]!r}"

    def test_concurrent_dump_string_is_isolated(self):
        """Parallel yaml_dump_string calls must not bleed state across threads."""
        results: list[str] = []
        errors: list[BaseException] = []

        def dump(worker: int) -> None:
            try:
                for _ in range(200):
                    out = yaml_dump_string({"worker": worker, "items": list(range(worker + 3))})
                    # Each call must produce exactly ONE document for ITS data.
                    assert out.count("worker:") == 1
                    assert f"worker: {worker}" in out
                    results.append(out)
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=dump, args=(w,)) for w in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, f"concurrent yaml_dump_string corrupted: {errors[0]!r}"

    def test_dump_is_atomic_no_temp_left(self, tmp_path):
        path = tmp_path / "sub" / "out.yaml"
        yaml_dump({"a": 1}, path)
        assert yaml_load(path) == {"a": 1}
        # No stray temp file left beside the target.
        leftovers = [p.name for p in (tmp_path / "sub").iterdir() if p.name != "out.yaml"]
        assert leftovers == []


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
