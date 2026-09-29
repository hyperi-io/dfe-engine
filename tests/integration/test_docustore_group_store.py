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
from prometheus_client.parser import text_string_to_metric_families
from pydantic import ValidationError
from scalo.logger import logger
from scalo.metrics import create_metrics

from dfe_engine.auth.breakglass import GROUP as RECOVERY_GROUP
from dfe_engine.auth.breakglass import USERNAME as BREAKGLASS
from dfe_engine.auth.groups import (
    GROUPS_SKIPPED,
    DocuStoreGroupStore,
    Group,
    GroupExistsError,
    GroupMetrics,
)
from dfe_engine.auth.oidc.adapters.base import OIDCGroupAdapter
from dfe_engine.auth.oidc.models import GroupInfo, GroupResolutionConfig, OIDCProvider
from dfe_engine.auth.oidc.registry import OIDCProviderRegistry
from dfe_engine.auth.oidc.sync import sync_provider
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


class TestAStoredDocumentThatIsNotAValidGroup:
    """One document no Group can be made from must not take every other group down with it."""

    BAD_SCOPE = "org:../elsewhere/outside"

    @pytest.fixture
    def manager(self):
        return create_metrics("test", backend="prometheus", enable_auto_update=False)

    @pytest.fixture
    def planted(self, docu, manager):
        """A record no create() could make, as a hand edit or an older engine leaves it."""
        store = DocuStoreGroupStore(docu, collection="groups", metrics=GroupMetrics(manager))
        store.create("analysts", ["data_analyst"], members=["bob"])
        docu.collection("groups").insert_one(
            {"name": "climber", "roles": ["admin"], "members": ["bob"], "scope": self.BAD_SCOPE}
        )
        return store

    def _skips(self, manager) -> float | None:
        for family in text_string_to_metric_families(manager.metrics_text):
            for sample in family.samples:
                if sample.name == GROUPS_SKIPPED and sample.labels == {"reason": "invalid"}:
                    return sample.value
        return None

    def test_every_other_group_still_loads_and_the_bad_one_is_absent(self, planted):
        assert [g.name for g in planted.list()] == ["analysts"]
        assert planted.get("climber") is None
        assert planted.resolve_roles_for_member("bob") == ["data_analyst"]

    def test_creating_one_over_it_is_refused_as_existing(self, planted, docu):
        with pytest.raises(GroupExistsError):
            planted.create("climber", [])

        stored = docu.collection("groups").find_one({"name": "climber"}, {"_id": 0})
        assert stored["scope"] == self.BAD_SCOPE

    def test_the_skip_is_logged_and_counted_once_until_it_loads_again(self, planted, docu, manager):
        seen: list[str] = []
        handler = logger.add(lambda m: seen.append(m.record["message"]), level="WARNING")
        try:
            planted.list()
            planted.list()
            planted.get("climber")
            docu.collection("groups").update_one({"name": "climber"}, {"$set": {"scope": "system"}})
            assert [g.name for g in planted.list()] == ["analysts", "climber"]
            docu.collection("groups").update_one(
                {"name": "climber"}, {"$set": {"scope": self.BAD_SCOPE}}
            )
            planted.list()
        finally:
            logger.remove(handler)

        assert seen.count("stored group skipped: not a valid group") == 2
        assert self._skips(manager) == 2

    def test_a_collection_that_does_not_opt_in_still_raises(self, planted, docu):
        """Accounts read through the same layer and keep raising on a document their model rejects."""
        with pytest.raises(ValidationError):
            docu.typed("groups", Group, key="name").list()


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
        store.update("with-src", source_id="prov-123")
        index = store.by_source_id()
        assert "prov-123" in index
        assert index["prov-123"].name == "with-src"
        assert all(g.source_id for g in index.values())


class TestTheProviderIdsAGroupCarries:
    """The OIDC group sync and SCIM write a group's provider ids through update().

    Role resolution finds a group by its source_id when the IdP asserts an id
    rather than a name, as Entra's object GUIDs are.
    """

    GUID = "7b1d0f3e-0000-4000-8000-000000000001"

    def test_update_keeps_them(self, store):
        store.create("entra-analysts", ["data_analyst"])

        store.update("entra-analysts", source_provider="entra", source_id=self.GUID)

        got = store.get("entra-analysts")
        assert (got.source_provider, got.source_id) == ("entra", self.GUID)
        assert store.by_source_id()[self.GUID].name == "entra-analysts"

    def test_a_field_outside_the_list_is_still_ignored(self, store):
        """Attributes have their own setter; update() leaves them alone."""
        store.create("entra-analysts", ["data_analyst"])

        store.update("entra-analysts", attributes={"k": "v"})

        assert store.get("entra-analysts").attributes == {}

    async def test_the_oidc_sync_records_them(self, store, tmp_path):
        provider = OIDCProvider(
            type="generic",
            enabled=True,
            issuer="https://sso.example.com",
            groups=GroupResolutionConfig(mode="api"),
        )
        registry = OIDCProviderRegistry(tmp_path / "oidc")
        registry.create("entra", provider)
        adapter = _Directory(provider, [GroupInfo(id=self.GUID, name="Analysts")])

        result = await sync_provider("entra", registry, store, adapter=adapter)

        assert result["created"] == 1
        got = store.get("analysts")
        assert (got.source_provider, got.source_id) == ("entra", self.GUID)

    async def test_the_oidc_sync_leaves_a_group_no_admin_linked(self, store, tmp_path):
        store.create(RECOVERY_GROUP, ["admin"])
        provider = OIDCProvider(
            type="entra_id",
            enabled=True,
            issuer="https://sso.example.com",
            groups=GroupResolutionConfig(mode="api"),
        )
        registry = OIDCProviderRegistry(tmp_path / "oidc")
        registry.create("entra", provider)
        adapter = _Directory(provider, [GroupInfo(id=self.GUID, name="DFE Admins")])

        result = await sync_provider("entra", registry, store, adapter=adapter)

        assert (result["updated"], result["groups_skipped"]) == (0, 1)
        got = store.get(RECOVERY_GROUP)
        assert (got.source_provider, got.source_id) == ("", "")
        assert self.GUID not in store.by_source_id()


class _Directory(OIDCGroupAdapter):
    """A provider directory holding a fixed list of groups."""

    def __init__(self, provider: OIDCProvider, groups: list[GroupInfo]) -> None:
        super().__init__(provider)
        self._groups = groups

    async def resolve_groups(self, subject: str) -> list[GroupInfo]:
        return []

    async def list_all_groups(self) -> list[GroupInfo]:
        return self._groups

    async def test_connection(self) -> tuple[bool, str]:
        return (True, "fixed directory")


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

    @pytest.mark.parametrize(
        "change",
        [
            pytest.param({"roles": ["infra_admin"]}, id="admin-role-dropped"),
            pytest.param({"scope": "org:acme"}, id="moved-to-an-org"),
        ],
    )
    def test_taking_the_admin_role_off_the_admin_group_is_refused(self, seeded, change):
        with pytest.raises(ProtectedAccountError):
            seeded.update(RECOVERY_GROUP, **change)
        stored = seeded.get(RECOVERY_GROUP)
        assert (stored.roles, stored.scope) == (["admin"], "system")

    def test_a_role_added_beside_admin_still_works(self, seeded):
        assert seeded.update(RECOVERY_GROUP, roles=["admin", "data_viewer"]).roles == [
            "admin",
            "data_viewer",
        ]
