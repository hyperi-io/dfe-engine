"""Integration tests for the document-store-backed group store + document layer.

Runs only when ``DFE_TEST_MONGO_URI`` points at a reachable document store / mongo-wire
server (the rig document store via port-forward, or a testcontainer in CI). Uses a
throwaway database per test so it never touches real data, and drops it on
teardown. Real dependency, no mocks - the name-validation, member de-duplication,
scope and delete-with-members semantics must hold against a real store exactly as
they do for the YAML backend.
"""

import os
import uuid

import pytest

from dfe_engine.auth.breakglass import GROUP as RECOVERY_GROUP
from dfe_engine.auth.breakglass import USERNAME as BREAKGLASS
from dfe_engine.auth.groups import DocuStoreGroupStore, Group
from dfe_engine.auth.protected_accounts import ProtectedAccountError
from dfe_engine.store.documents import DocuStore

pytestmark = pytest.mark.integration

_URI = os.environ.get("DFE_TEST_MONGO_URI", "")


@pytest.fixture
def docu():
    if not _URI:
        pytest.skip("DFE_TEST_MONGO_URI not set (needs a reachable document store)")
    db_name = f"dfe_engine_test_{uuid.uuid4().hex[:8]}"
    doc = DocuStore(_URI, db_name)
    doc.ping()  # fail fast if the server is unreachable / auth wrong
    try:
        yield doc
    finally:
        doc.drop_database(db_name)
        doc.close()


@pytest.fixture
def store(docu):
    return DocuStoreGroupStore(docu, collection="groups")


class TestANameNoGroupCanHave:
    """Both backends share one name rule, so a lookup here never matches a name create refuses."""

    NAME = "../groups/dfe-admins"

    @pytest.fixture
    def planted(self, docu, store):
        """A record no create() could make, as a hand edit or an older engine leaves it."""
        docu.collection("groups").insert_one(Group(name=self.NAME, roles=["admin"]).model_dump())
        return store

    def test_get_misses(self, planted):
        assert planted.get(self.NAME) is None

    @pytest.mark.parametrize(
        "change",
        [
            pytest.param(lambda s, n: s.update(n, roles=[]), id="update"),
            pytest.param(lambda s, n: s.set_attributes(n, {"k": "v"}), id="set_attributes"),
            pytest.param(lambda s, n: s.delete(n), id="delete"),
            pytest.param(lambda s, n: s.add_member(n, "mallory"), id="add_member"),
            pytest.param(lambda s, n: s.remove_member(n, "mallory"), id="remove_member"),
        ],
    )
    def test_a_change_raises_and_leaves_the_record(self, planted, docu, change):
        before = docu.collection("groups").find_one({"name": self.NAME}, {"_id": 0})

        with pytest.raises(KeyError):
            change(planted, self.NAME)

        after = docu.collection("groups").find_one({"name": self.NAME}, {"_id": 0})
        assert after == before


class TestDocuStoreGroupStore:
    """Behaviour parity with the YAML GroupStore, against a real document store."""

    def test_create_and_get(self, store):
        group = store.create("admins", ["role-a", "role-b"], "The admins")
        assert group.name == "admins"
        assert group.roles == ["role-a", "role-b"]
        assert group.description == "The admins"
        got = store.get("admins")
        assert got is not None
        assert got.name == "admins"
        assert got.roles == ["role-a", "role-b"]

    def test_create_dedupes_members(self, store):
        group = store.create("ops", ["r1"], members=["u1", "u2", "u1"])
        assert group.members == ["u1", "u2"]

    def test_create_duplicate_raises(self, store):
        store.create("dup", ["r1"])
        with pytest.raises(ValueError):
            store.create("dup", ["r2"])

    def test_create_rejects_unsafe_name(self, store):
        with pytest.raises(ValueError):
            store.create("../evil", ["r1"])

    def test_create_rejects_invalid_scope(self, store):
        with pytest.raises(ValueError):
            store.create("bad", ["r1"], scope="not-a-scope")

    def test_create_org_scope(self, store):
        group = store.create("org-grp", ["r1"], scope="org:acme")
        assert group.scope == "org:acme"
        assert group.scope_org == "acme"

    def test_get_missing_returns_none(self, store):
        assert store.get("nobody") is None

    def test_list_sorted_by_name(self, store):
        store.create("g2", ["r1"])
        store.create("g1", ["r1"])
        assert [g.name for g in store.list()] == ["g1", "g2"]

    def test_update_fields(self, store):
        store.create("carol", ["r1"], "old")
        updated = store.update("carol", roles=["r2", "r3"], description="new", org_ids=["o1"])
        assert updated.roles == ["r2", "r3"]
        assert updated.description == "new"
        assert updated.org_ids == ["o1"]
        assert store.get("carol").description == "new"

    def test_update_scope(self, store):
        store.create("scoped", ["r1"])
        updated = store.update("scoped", scope="org:acme")
        assert updated.scope == "org:acme"

    def test_update_invalid_scope_raises(self, store):
        store.create("scoped2", ["r1"])
        with pytest.raises(ValueError):
            store.update("scoped2", scope="bogus")

    def test_update_missing_raises(self, store):
        with pytest.raises(KeyError):
            store.update("ghost", roles=["r1"])

    def test_delete(self, store):
        store.create("erin", ["r1"])
        store.delete("erin")
        assert store.get("erin") is None

    def test_delete_missing_raises(self, store):
        with pytest.raises(KeyError):
            store.delete("ghost")

    def test_delete_with_members_raises(self, store):
        store.create("staffed", ["r1"], members=["u1"])
        with pytest.raises(ValueError):
            store.delete("staffed")

    def test_add_member_idempotent(self, store):
        store.create("team", ["r1"])
        store.add_member("team", "u1")
        store.add_member("team", "u1")
        assert store.get("team").members == ["u1"]

    def test_add_member_missing_group_raises(self, store):
        with pytest.raises(KeyError):
            store.add_member("ghost", "u1")

    def test_remove_member(self, store):
        store.create("team2", ["r1"], members=["u1", "u2"])
        store.remove_member("team2", "u1")
        assert store.get("team2").members == ["u2"]

    def test_remove_member_absent_is_noop(self, store):
        store.create("team3", ["r1"], members=["u1"])
        store.remove_member("team3", "u2")
        assert store.get("team3").members == ["u1"]

    def test_remove_member_missing_group_raises(self, store):
        with pytest.raises(KeyError):
            store.remove_member("ghost", "u1")

    def test_resolve_roles_for_member(self, store):
        store.create("g-a", ["role-x", "role-y"], members=["alice"])
        store.create("g-b", ["role-y", "role-z"], members=["alice"])
        store.create("g-c", ["role-w"], members=["bob"])
        assert store.resolve_roles_for_member("alice") == ["role-x", "role-y", "role-z"]
        assert store.resolve_roles_for_member("bob") == ["role-w"]
        assert store.resolve_roles_for_member("nobody") == []

    def test_by_source_id(self, store):
        store.create("with-src", ["r1"])
        store.create("plain", ["r1"])
        # source_id is sync-owned (no public setter), so set it via a direct put
        # on the underlying typed collection to exercise the index.
        grp = store.get("with-src").model_copy(update={"source_id": "prov-123"})
        store._c.put("with-src", grp)
        index = store.by_source_id()
        assert "prov-123" in index
        assert index["prov-123"].name == "with-src"
        assert all(g.source_id for g in index.values())


class TestProtectedNameFloor:
    """The floor from issue #505 holds on this backend exactly as on the YAML one."""

    @pytest.fixture
    def seeded(self, store):
        store.create(RECOVERY_GROUP, ["admin"], members=["admin", BREAKGLASS, "alice"])
        store.create("dfe-viewers", ["data_viewer"], members=[BREAKGLASS])
        return store

    @pytest.mark.parametrize("username", ["admin", BREAKGLASS])
    def test_remove_member_from_the_admin_group_is_refused(self, seeded, username):
        with pytest.raises(ProtectedAccountError):
            seeded.remove_member(RECOVERY_GROUP, username)
        assert username in seeded.get(RECOVERY_GROUP).members

    @pytest.mark.parametrize("username", ["admin", BREAKGLASS])
    def test_member_replacement_that_drops_it_is_refused(self, seeded, username):
        with pytest.raises(ProtectedAccountError):
            seeded.update(RECOVERY_GROUP, members=["alice"])
        assert username in seeded.get(RECOVERY_GROUP).members

    def test_removing_an_unprotected_member_still_works(self, seeded):
        seeded.remove_member(RECOVERY_GROUP, "alice")
        assert "alice" not in seeded.get(RECOVERY_GROUP).members

    def test_removing_a_protected_member_from_another_group_still_works(self, seeded):
        seeded.remove_member("dfe-viewers", BREAKGLASS)
        assert seeded.get("dfe-viewers").members == []

    def test_allow_protected_reaches_past_the_floor(self, seeded):
        seeded.remove_member(RECOVERY_GROUP, BREAKGLASS, allow_protected=True)
        assert BREAKGLASS not in seeded.get(RECOVERY_GROUP).members
