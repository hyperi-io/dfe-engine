"""Integration tests for flexible attributes against a real document store.

Runs only when ``DFE_TEST_MONGO_URI`` points at a reachable document store / mongo-wire
server (the rig document store via port-forward, or a testcontainer in CI). Uses a
throwaway database per test so it never touches real data, and drops it on teardown.
Real dependency, no mocks - the inline ``set_attributes`` and the separate sensitive
store must behave against a real store exactly as they do for the YAML backend.
"""

from __future__ import annotations

import os
import uuid

import pytest

from dfe_engine.auth.accounts import DocuStoreAccountStore
from dfe_engine.auth.attributes import DocuStoreAttributeStore
from dfe_engine.auth.groups import DocuStoreGroupStore
from dfe_engine.store.documents import DocuStore

pytestmark = pytest.mark.integration

_URI = os.environ.get("DFE_TEST_MONGO_URI", "")
_NESTED = {"team": {"role": "lead"}, "tags": ["a", "b"]}


@pytest.fixture
def doc():
    if not _URI:
        pytest.skip("DFE_TEST_MONGO_URI not set (needs a reachable document store)")
    db_name = f"dfe_engine_test_{uuid.uuid4().hex[:8]}"
    store = DocuStore(_URI, db_name)
    store.ping()  # fail fast if the server is unreachable / auth wrong
    try:
        yield store
    finally:
        store.drop_database(db_name)
        store.close()


class TestDocuStoreInlineAttributes:
    """Inline non-sensitive attributes round-trip on the document-store stores."""

    def test_account_set_attributes(self, doc):
        store = DocuStoreAccountStore(doc, collection="accounts")
        store.create("alice", "pw-Aa1")
        store.set_attributes("alice", _NESTED)
        assert store.get("alice").attributes == _NESTED

    def test_account_set_attributes_full_replace(self, doc):
        store = DocuStoreAccountStore(doc, collection="accounts")
        store.create("alice", "pw-Aa1")
        store.set_attributes("alice", {"first": 1})
        store.set_attributes("alice", {"second": 2})
        assert store.get("alice").attributes == {"second": 2}

    def test_account_set_attributes_missing_raises(self, doc):
        store = DocuStoreAccountStore(doc, collection="accounts")
        with pytest.raises(KeyError):
            store.set_attributes("ghost", _NESTED)

    def test_group_set_attributes(self, doc):
        store = DocuStoreGroupStore(doc, collection="groups")
        store.create("admins", roles=["admin"])
        store.set_attributes("admins", _NESTED)
        assert store.get("admins").attributes == _NESTED

    def test_group_set_attributes_missing_raises(self, doc):
        store = DocuStoreGroupStore(doc, collection="groups")
        with pytest.raises(KeyError):
            store.set_attributes("ghost", _NESTED)


class TestDocuStoreAttributeStore:
    """Sensitive-attribute keyed store against a real document store."""

    def test_get_absent_returns_empty(self, doc):
        store = DocuStoreAttributeStore(doc, collection="sensitive_account_attributes")
        assert store.get("alice") == {}

    def test_put_then_get_round_trips_nested(self, doc):
        store = DocuStoreAttributeStore(doc, collection="sensitive_account_attributes")
        store.put("alice", _NESTED)
        assert store.get("alice") == _NESTED

    def test_put_full_replace(self, doc):
        store = DocuStoreAttributeStore(doc, collection="sensitive_account_attributes")
        store.put("alice", {"first": 1})
        store.put("alice", {"second": 2})
        assert store.get("alice") == {"second": 2}

    def test_delete_removes(self, doc):
        store = DocuStoreAttributeStore(doc, collection="sensitive_account_attributes")
        store.put("alice", _NESTED)
        store.delete("alice")
        assert store.get("alice") == {}

    def test_delete_absent_is_noop(self, doc):
        store = DocuStoreAttributeStore(doc, collection="sensitive_account_attributes")
        store.delete("nobody")  # must not raise

    def test_isolated_by_entity_id(self, doc):
        store = DocuStoreAttributeStore(doc, collection="sensitive_group_attributes")
        store.put("alice", {"a": 1})
        store.put("bob", {"b": 2})
        assert store.get("alice") == {"a": 1}
        assert store.get("bob") == {"b": 2}
