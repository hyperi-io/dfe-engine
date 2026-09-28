"""Tests for yaml_utils — deep_merge and YAML operations."""

import os
import subprocess
import sys
import threading
from enum import StrEnum

import pytest
from prometheus_client.parser import text_string_to_metric_families
from ruamel.yaml.scalarstring import LiteralScalarString
from scalo.metrics import create_metrics

from dfe_engine.yaml_health import WRITE_FAILURES, YamlWriteMetrics, write_health
from dfe_engine.yaml_utils import (
    YamlWriteError,
    deep_merge,
    literal_block,
    yaml_dump,
    yaml_dump_string,
    yaml_load,
    yaml_load_string,
)

# A grok line past ruamel's 80-column default, shaped like the bundled filebeat
# pipeline. The repeated chunk carries the escape pair a fold splits.
_CHUNK = r"%{DATA:source.address}\(%{DATA:source.port}\) "
_LONG_LINE = (
    "    value, err = parse_groks(value: .message, patterns: [s'" + (_CHUNK * 2) + ".*?$'])"
)
_BODY = "# a transform file\n" + _LONG_LINE + "\n"


class TestLongScalarsAreNotFolded:
    """A stored file body must come back exactly as written.

    A scalar folded at ruamel's 80-column default returns with each fold turned
    into a space, which splits an escape pair like ``\\)`` into ``\\ )``.
    """

    def test_a_plain_string_is_never_folded(self):
        # literal_block returns content unchanged when the block form is unsafe,
        # so the emitter's quoted fallback has to be lossless as well.
        body = _LONG_LINE + "   \n"  # trailing space rules out the block form
        back = yaml_load_string(yaml_dump_string({"content": body}))
        assert back["content"] == body

    def test_a_file_set_entry_survives_a_second_write(self):
        # A file body is stored once and re-serialised by every later commit to
        # the same overlay, so one lossless write is not enough.
        body = _LONG_LINE + "   \n"
        doc = {"transformFiles": [{"name": "t.vrl", "content": literal_block(body)}]}
        once = yaml_load_string(yaml_dump_string(doc))
        once["replicaCount"] = 2
        twice = yaml_load_string(yaml_dump_string(once))
        assert twice["transformFiles"][0]["content"] == body

    def test_the_block_form_still_round_trips(self):
        doc = {"transformFiles": [{"name": "t.vrl", "content": literal_block(_BODY)}]}
        back = yaml_load_string(yaml_dump_string(doc))
        assert back["transformFiles"][0]["content"].rstrip("\n") == _BODY.rstrip("\n")


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


class _Colour(StrEnum):
    RED = "red"


def _nested(depth: int) -> dict:
    """A mapping nested *depth* levels deep."""
    doc: dict = {"leaf": "x"}
    for _ in range(depth - 1):
        doc = {"k": doc}
    return doc


# Data the writer raises on: a type it cannot represent, and nesting past the recursion limit.
_UNDUMPABLE = [
    pytest.param({"v": _Colour.RED}, id="unrepresentable"),
    pytest.param(_nested(1000), id="nested-past-the-recursion-limit"),
]

# Dumps cleanly but reads back as something else: a block scalar cannot hold a CR, and NEL
# is written as a character the reader takes for a line break.
_UNREADABLE = [
    pytest.param({"v": LiteralScalarString("a\r\nb\n")}, id="carriage-return-in-a-block"),
    pytest.param({"v": "\x85"}, id="next-line-character"),
]


@pytest.fixture
def reported():
    """A real metrics backend bound to the writer; tests/unit/conftest.py unbinds it after."""
    manager = create_metrics("test", backend="prometheus", enable_auto_update=False)
    write_health().bind(YamlWriteMetrics(manager))
    return manager


def _failures(manager, reason: str) -> float:
    for family in text_string_to_metric_families(manager.metrics_text):
        for sample in family.samples:
            if sample.name == WRITE_FAILURES and sample.labels.get("reason") == reason:
                return sample.value
    return 0.0


class TestAFailedDumpDoesNotBreakLaterWrites:
    """ruamel keeps a failed dump's open document on the thread's writer.

    Every later dump on that thread then wrote nothing: a file write replaced the file
    with an empty one and reported success, and a string dump returned ''.
    """

    @pytest.mark.parametrize("bad", _UNDUMPABLE)
    def test_the_next_file_write_keeps_its_content(self, tmp_path, reported, bad):
        path = tmp_path / "groups.yaml"

        with pytest.raises(YamlWriteError) as refused:
            yaml_dump(bad, tmp_path / "bad.yaml")
        yaml_dump({"members": ["alice"]}, path)

        assert refused.value.reason == "dump"
        assert path.read_text(encoding="utf-8") == "members:\n  - alice\n"

    @pytest.mark.parametrize("bad", _UNDUMPABLE)
    def test_the_next_string_dump_returns_its_text(self, reported, bad):
        with pytest.raises(YamlWriteError):
            yaml_dump_string(bad)

        assert yaml_dump_string({"a": 1}) == "a: 1\n"


class TestAWriteIsReadBackBeforeItLands:
    @pytest.mark.parametrize("bad", _UNREADABLE)
    def test_yaml_that_reads_back_differently_is_refused_and_the_old_file_kept(
        self, tmp_path, reported, bad
    ):
        path = tmp_path / "group.yaml"
        path.write_text("old: content\n", encoding="utf-8")

        with pytest.raises(YamlWriteError) as refused:
            yaml_dump(bad, path)

        assert refused.value.reason == "verify"
        assert path.read_text(encoding="utf-8") == "old: content\n"
        assert [p.name for p in tmp_path.iterdir()] == ["group.yaml"]

    @pytest.mark.parametrize("bad", _UNREADABLE)
    def test_a_string_that_reads_back_differently_is_refused(self, reported, bad):
        with pytest.raises(YamlWriteError) as refused:
            yaml_dump_string(bad)

        assert refused.value.reason == "verify"

    def test_values_that_read_back_as_equal_data_still_write(self, tmp_path):
        """A tuple reads back as a list and NaN never equals itself; neither is a refusal."""
        path = tmp_path / "doc.yaml"

        yaml_dump({"pair": (1, 2), "ratio": float("nan"), "tags": {"a"}}, path)

        back = yaml_load(path)
        assert back["pair"] == [1, 2]
        assert back["ratio"] != back["ratio"]
        assert back["tags"] == {"a"}


class TestARefusedWriteIsReported:
    def test_each_refusal_is_counted_by_reason(self, tmp_path, reported):
        for bad in ({"v": _Colour.RED}, {"v": "\x85"}, {"v": LiteralScalarString("a\r\n")}):
            with pytest.raises(YamlWriteError):
                yaml_dump(bad, tmp_path / "doc.yaml")

        assert _failures(reported, "dump") == 1.0
        assert _failures(reported, "verify") == 2.0

    def test_a_refused_file_is_degraded_until_it_next_writes(self, tmp_path, reported):
        path = tmp_path / "doc.yaml"

        with pytest.raises(YamlWriteError):
            yaml_dump({"v": "\x85"}, path)
        refused = write_health().degraded()
        yaml_dump({"v": "fine"}, path)

        assert [(e.target, e.reason) for e in refused] == [(str(path), "verify")]
        assert write_health().degraded() == []

    def test_a_string_dump_with_no_target_is_counted_but_degrades_nothing(self, reported):
        with pytest.raises(YamlWriteError):
            yaml_dump_string({"v": "\x85"})

        assert _failures(reported, "verify") == 1.0
        assert write_health().degraded() == []


def test_files_are_read_and_written_as_utf8_whatever_the_locale(tmp_path):
    """Under a C locale with UTF-8 mode off, the default text encoding is ASCII."""
    path = tmp_path / "doc.yaml"
    script = (
        "import sys; from dfe_engine.yaml_utils import yaml_dump, yaml_load; "
        "city = 'Li\\u00e8ge'; yaml_dump({'city': city}, sys.argv[1]); "
        "print(yaml_load(sys.argv[1])['city'] == city)"
    )
    env = {**os.environ, "LC_ALL": "C", "PYTHONUTF8": "0"}

    result = subprocess.run(
        [sys.executable, "-X", "utf8=0", "-c", script, str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "True"
    assert path.read_bytes() == b"city: Li\xc3\xa8ge\n"


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
