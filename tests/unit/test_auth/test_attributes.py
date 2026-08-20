#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_attributes.py
#  Purpose:      Tests for flexible attributes (inline non-sensitive + sensitive store)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

import pytest

from dfe_engine.auth.accounts import Account, AccountStore
from dfe_engine.auth.attributes import AttributeStore
from dfe_engine.auth.groups import Group, GroupStore

_NESTED = {"team": {"role": "lead"}, "tags": ["a", "b"]}


# ---------------------------------------------------------------------------
# Inline non-sensitive attributes -- nested-dict round-trip
# ---------------------------------------------------------------------------


class TestAccountInlineAttributes:
    def test_default_empty(self, tmp_path):
        store = AccountStore(tmp_path / "accounts")
        acct = store.create("alice", "pw-Aa1")
        assert acct.attributes == {}

    def test_nested_dict_round_trips(self, tmp_path):
        store = AccountStore(tmp_path / "accounts")
        store.create("alice", "pw-Aa1")
        store.set_attributes("alice", _NESTED)
        got = store.get("alice")
        assert got is not None
        assert got.attributes == _NESTED
        assert got.attributes["team"]["role"] == "lead"
        assert got.attributes["tags"] == ["a", "b"]

    def test_set_attributes_full_replace(self, tmp_path):
        store = AccountStore(tmp_path / "accounts")
        store.create("alice", "pw-Aa1")
        store.set_attributes("alice", {"first": 1})
        store.set_attributes("alice", {"second": 2})
        assert store.get("alice").attributes == {"second": 2}

    def test_set_attributes_bumps_updated_at(self, tmp_path):
        store = AccountStore(tmp_path / "accounts")
        created = store.create("alice", "pw-Aa1")
        updated = store.set_attributes("alice", _NESTED)
        assert updated.updated_at >= created.updated_at

    def test_set_attributes_missing_raises(self, tmp_path):
        store = AccountStore(tmp_path / "accounts")
        with pytest.raises(KeyError):
            store.set_attributes("ghost", _NESTED)

    def test_model_carries_attributes(self):
        acct = Account(username="a", password_hash="!", attributes=_NESTED)
        assert acct.attributes == _NESTED


class TestGroupInlineAttributes:
    def test_default_empty(self, tmp_path):
        store = GroupStore(tmp_path / "groups")
        group = store.create("admins", roles=["admin"])
        assert group.attributes == {}

    def test_nested_dict_round_trips(self, tmp_path):
        store = GroupStore(tmp_path / "groups")
        store.create("admins", roles=["admin"])
        store.set_attributes("admins", _NESTED)
        got = store.get("admins")
        assert got is not None
        assert got.attributes == _NESTED
        assert got.attributes["team"]["role"] == "lead"
        assert got.attributes["tags"] == ["a", "b"]

    def test_set_attributes_full_replace(self, tmp_path):
        store = GroupStore(tmp_path / "groups")
        store.create("admins", roles=["admin"])
        store.set_attributes("admins", {"first": 1})
        store.set_attributes("admins", {"second": 2})
        assert store.get("admins").attributes == {"second": 2}

    def test_set_attributes_missing_raises(self, tmp_path):
        store = GroupStore(tmp_path / "groups")
        with pytest.raises(KeyError):
            store.set_attributes("ghost", _NESTED)

    def test_model_carries_attributes(self):
        group = Group(name="g", attributes=_NESTED)
        assert group.attributes == _NESTED


# ---------------------------------------------------------------------------
# Sensitive attributes -- separate YAML-backed keyed store
# ---------------------------------------------------------------------------


class TestAttributeStore:
    def test_get_absent_returns_empty(self, tmp_path):
        store = AttributeStore(tmp_path / "sensitive")
        assert store.get("alice") == {}

    def test_put_then_get_round_trips_nested(self, tmp_path):
        store = AttributeStore(tmp_path / "sensitive")
        store.put("alice", _NESTED)
        assert store.get("alice") == _NESTED

    def test_put_full_replace(self, tmp_path):
        store = AttributeStore(tmp_path / "sensitive")
        store.put("alice", {"first": 1})
        store.put("alice", {"second": 2})
        assert store.get("alice") == {"second": 2}

    def test_put_empty_dict(self, tmp_path):
        store = AttributeStore(tmp_path / "sensitive")
        store.put("alice", {})
        assert store.get("alice") == {}

    def test_delete_removes(self, tmp_path):
        store = AttributeStore(tmp_path / "sensitive")
        store.put("alice", _NESTED)
        store.delete("alice")
        assert store.get("alice") == {}

    def test_delete_absent_is_noop(self, tmp_path):
        store = AttributeStore(tmp_path / "sensitive")
        store.delete("nobody")  # must not raise

    def test_isolated_by_entity_id(self, tmp_path):
        store = AttributeStore(tmp_path / "sensitive")
        store.put("alice", {"a": 1})
        store.put("bob", {"b": 2})
        assert store.get("alice") == {"a": 1}
        assert store.get("bob") == {"b": 2}

    @pytest.mark.parametrize("bad", ["../evil", "a/b", "with space", ".hidden", ""])
    def test_rejects_unsafe_id(self, tmp_path, bad):
        store = AttributeStore(tmp_path / "sensitive")
        with pytest.raises(ValueError):
            store.put(bad, {"x": 1})
        with pytest.raises(ValueError):
            store.get(bad)
        with pytest.raises(ValueError):
            store.delete(bad)
