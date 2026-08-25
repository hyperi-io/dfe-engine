#  Project:      dfe-engine
#  File:         test_repository_store.py
#  Purpose:      RepositoryStore unit tests over an in-memory fake CH client
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""RepositoryStore behaviour: roundtrip, latest-wins, tombstones, etags, merge patch."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from common.fake_repository_ch import FakeRepositoryCH

from dfe_engine.repository import store as store_module
from dfe_engine.repository.store import ConflictError, RepositoryStore, json_merge_patch


@pytest.fixture(autouse=True)
def _reset_schema_flag():
    """ensure_schema is once-per-process; reset so each test sees fresh DDL."""
    RepositoryStore._ensured_databases.clear()
    yield
    RepositoryStore._ensured_databases.clear()


@pytest.fixture(autouse=True)
def _deterministic_clock(monkeypatch: pytest.MonkeyPatch):
    """Strictly increasing ms timestamps so latest-wins and etags are deterministic."""
    base = datetime(2026, 7, 3, 12, 0, 0, tzinfo=UTC)
    ticks = iter(range(1, 10_000))

    def fake_now_ms() -> datetime:
        return base + timedelta(milliseconds=next(ticks))

    monkeypatch.setattr(store_module, "_now_ms", fake_now_ms)


@pytest.fixture
def ch() -> FakeRepositoryCH:
    return FakeRepositoryCH()


@pytest.fixture
def store(ch: FakeRepositoryCH) -> RepositoryStore:
    return RepositoryStore(ch)


class TestEnsureSchema:
    def test_runs_ddl_once_per_process(self, ch: FakeRepositoryCH, store: RepositoryStore):
        store.ensure_schema()
        assert len(ch.ddl) == 2
        assert ch.ddl[0].startswith("CREATE DATABASE IF NOT EXISTS dfe")
        # The rendered CREATE leads with its generated comment header.
        assert "CREATE TABLE IF NOT EXISTS dfe.repository" in ch.ddl[1]
        # Second store instance in the same process: no re-run
        RepositoryStore(ch).ensure_schema()
        assert len(ch.ddl) == 2

    def test_database_override_substitutes_name(self, ch: FakeRepositoryCH):
        RepositoryStore(ch, database="custom_db").ensure_schema()
        assert "custom_db.repository" in ch.ddl[1]
        assert "custom_db" in ch.ddl[0]

    def test_lazy_on_first_read(self, ch: FakeRepositoryCH, store: RepositoryStore):
        assert ch.ddl == []
        store.get("user", "alice", "preferences", "default")
        assert len(ch.ddl) == 2


class TestPutGet:
    def test_roundtrip(self, store: RepositoryStore):
        meta = store.put(
            "user",
            "alice",
            "preferences",
            "default",
            b'{"theme":"dark"}',
            "application/json",
            "alice",
        )
        assert meta["size"] == len(b'{"theme":"dark"}')
        record = store.get("user", "alice", "preferences", "default")
        assert record is not None
        assert record["value"] == b'{"theme":"dark"}'
        assert record["content_type"] == "application/json"
        assert record["updated_by"] == "alice"
        assert record["etag"] == meta["etag"]

    def test_latest_wins_on_second_put(self, store: RepositoryStore):
        store.put("user", "alice", "ns", "k", b"one", "text/plain", "alice")
        meta2 = store.put("user", "alice", "ns", "k", b"two", "text/plain", "alice")
        record = store.get("user", "alice", "ns", "k")
        assert record is not None
        assert record["value"] == b"two"
        assert record["etag"] == meta2["etag"]

    def test_get_absent_returns_none(self, store: RepositoryStore):
        assert store.get("user", "alice", "ns", "missing") is None

    def test_parameter_binding_used(self, ch: FakeRepositoryCH, store: RepositoryStore):
        store.get("user", "alice'; DROP TABLE x", "ns", "k")
        assert ch.select_params[-1]["scope_id"] == "alice'; DROP TABLE x"


class TestDelete:
    def test_tombstone_hides_row(self, store: RepositoryStore):
        store.put("org", "acme", "ns", "k", b"v", "text/plain", "admin")
        assert store.delete("org", "acme", "ns", "k") is True
        assert store.get("org", "acme", "ns", "k") is None
        assert store.list("org", "acme", "ns") == []

    def test_delete_absent_returns_false(self, store: RepositoryStore):
        assert store.delete("org", "acme", "ns", "missing") is False

    def test_put_after_delete_resurrects(self, store: RepositoryStore):
        store.put("org", "acme", "ns", "k", b"v1", "text/plain", "admin")
        store.delete("org", "acme", "ns", "k")
        store.put("org", "acme", "ns", "k", b"v2", "text/plain", "admin")
        record = store.get("org", "acme", "ns", "k")
        assert record is not None
        assert record["value"] == b"v2"


class TestList:
    def test_list_metadata(self, store: RepositoryStore):
        store.put("org", "acme", "ns", "b", b"bb", "text/plain", "admin")
        store.put("org", "acme", "ns", "a", b'{"x":1}', "application/json", "alice")
        store.put("org", "acme", "other", "c", b"cc", "text/plain", "admin")
        entries = store.list("org", "acme", "ns")
        assert [e["key"] for e in entries] == ["a", "b"]
        assert entries[0]["content_type"] == "application/json"
        assert entries[0]["size"] == len(b'{"x":1}')
        assert entries[0]["updated_by"] == "alice"
        assert entries[0]["etag"]

    def test_list_hides_tombstones(self, store: RepositoryStore):
        store.put("org", "acme", "ns", "a", b"aa", "text/plain", "admin")
        store.put("org", "acme", "ns", "b", b"bb", "text/plain", "admin")
        store.delete("org", "acme", "ns", "a")
        assert [e["key"] for e in store.list("org", "acme", "ns")] == ["b"]


class TestIfMatch:
    def test_if_match_mismatch_raises(self, store: RepositoryStore):
        store.put("user", "alice", "ns", "k", b"one", "text/plain", "alice")
        meta2 = store.put("user", "alice", "ns", "k", b"two", "text/plain", "alice")
        with pytest.raises(ConflictError) as exc_info:
            store.put(
                "user", "alice", "ns", "k", b"three", "text/plain", "alice", if_match="stale-etag"
            )
        assert exc_info.value.current_etag == meta2["etag"]

    def test_if_match_current_etag_succeeds(self, store: RepositoryStore):
        meta = store.put("user", "alice", "ns", "k", b"one", "text/plain", "alice")
        store.put("user", "alice", "ns", "k", b"two", "text/plain", "alice", if_match=meta["etag"])
        record = store.get("user", "alice", "ns", "k")
        assert record is not None
        assert record["value"] == b"two"

    def test_if_match_on_absent_row_raises(self, store: RepositoryStore):
        with pytest.raises(ConflictError) as exc_info:
            store.put("user", "alice", "ns", "new", b"v", "text/plain", "alice", if_match="etag")
        assert exc_info.value.current_etag is None


class TestJsonMergePatch:
    def test_null_deletes_key(self):
        assert json_merge_patch({"a": 1, "b": 2}, {"b": None}) == {"a": 1}

    def test_nested_merge(self):
        target = {"ui": {"theme": "light", "density": "compact"}, "lang": "en"}
        patch = {"ui": {"theme": "dark"}}
        assert json_merge_patch(target, patch) == {
            "ui": {"theme": "dark", "density": "compact"},
            "lang": "en",
        }

    def test_nested_null_deletes(self):
        assert json_merge_patch({"ui": {"a": 1, "b": 2}}, {"ui": {"a": None}}) == {"ui": {"b": 2}}

    def test_array_replaces(self):
        assert json_merge_patch({"tags": [1, 2, 3]}, {"tags": [9]}) == {"tags": [9]}

    def test_scalar_replaces_object(self):
        assert json_merge_patch({"a": {"deep": 1}}, {"a": "flat"}) == {"a": "flat"}

    def test_non_dict_patch_replaces_wholesale(self):
        assert json_merge_patch({"a": 1}, [1, 2]) == [1, 2]

    def test_null_on_missing_key_is_noop(self):
        assert json_merge_patch({"a": 1}, {"zz": None}) == {"a": 1}
