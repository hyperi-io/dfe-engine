"""Integration tests for the document-store-backed account store + document layer.

Runs only when ``DFE_TEST_MONGO_URI`` points at a reachable document store / mongo-wire
server (the rig document store via port-forward, or a testcontainer in CI). Uses a
throwaway database per test so it never touches real data, and drops it on
teardown. Real dependency, no mocks - the timing-safe and unusable-password
semantics must hold against a real store exactly as they do for the YAML backend.
"""

from __future__ import annotations

import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from dfe_engine.auth.accounts import Account, DocuStoreAccountStore
from dfe_engine.auth.breakglass import GROUP as RECOVERY_GROUP
from dfe_engine.auth.breakglass import USERNAME as BREAKGLASS
from dfe_engine.auth.groups import GroupStore
from dfe_engine.auth.jit import JitIdentityCollisionError, JitProvisioner
from dfe_engine.auth.protected_accounts import ProtectedAccountError
from dfe_engine.auth.scim_mapping import SCIM_SOURCE_PROVIDER
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
    return DocuStoreAccountStore(docu, collection="accounts")


class TestANameNoAccountCanHave:
    """Both backends share one name rule, so a lookup here never matches a name create refuses."""

    NAME = "../accounts/alice"

    @pytest.fixture
    def planted(self, docu, store):
        """A record no create() could make, as a hand edit or an older engine leaves it."""
        record = Account(username=self.NAME, password_hash="!")
        docu.collection("accounts").insert_one(record.model_dump())
        return store

    def test_get_misses(self, planted):
        assert planted.get(self.NAME) is None

    @pytest.mark.parametrize(
        "change",
        [
            pytest.param(lambda s, n: s.update(n, enabled=False), id="update"),
            pytest.param(lambda s, n: s.reset_password(n, "another-Pw-1"), id="reset_password"),
            pytest.param(lambda s, n: s.set_attributes(n, {"k": "v"}), id="set_attributes"),
            pytest.param(lambda s, n: s.delete(n), id="delete"),
        ],
    )
    def test_a_change_raises_and_leaves_the_record(self, planted, docu, change):
        before = docu.collection("accounts").find_one({"username": self.NAME}, {"_id": 0})

        with pytest.raises(KeyError):
            change(planted, self.NAME)

        after = docu.collection("accounts").find_one({"username": self.NAME}, {"_id": 0})
        assert after == before


class TestDocuStoreAccountStore:
    """Behaviour parity with the YAML AccountStore, against a real document store."""

    def test_create_and_get(self, store):
        acct = store.create("alice", "s3cret-Pw", groups=["dfe-admins"])
        assert acct.username == "alice"
        assert acct.groups == ["dfe-admins"]
        got = store.get("alice")
        assert got is not None
        assert got.username == "alice"
        assert got.password_hash.startswith("$2")  # bcrypt, not plaintext

    def test_create_duplicate_raises(self, store):
        store.create("bob", "pw-Aa1")
        with pytest.raises(ValueError):
            store.create("bob", "other-Pw")

    def test_create_rejects_unsafe_name(self, store):
        with pytest.raises(ValueError):
            store.create("../evil", "pw-Aa1")

    def test_get_missing_returns_none(self, store):
        assert store.get("nobody") is None

    def test_list_sorted_by_username(self, store):
        store.create("u2", "pw-Aa1")
        store.create("u1", "pw-Aa1")
        assert [a.username for a in store.list()] == ["u1", "u2"]

    def test_update_groups_and_enabled(self, store):
        store.create("carol", "pw-Aa1", groups=["g1"])
        updated = store.update("carol", groups=["g2", "g3"], enabled=False)
        assert updated.groups == ["g2", "g3"]
        assert updated.enabled is False
        assert store.get("carol").enabled is False

    def test_update_missing_raises(self, store):
        with pytest.raises(KeyError):
            store.update("ghost", enabled=False)

    def test_reset_password_swaps_credential(self, store):
        store.create("dave", "old-Pw-1")
        assert store.verify_password("dave", "old-Pw-1")
        store.reset_password("dave", "new-Pw-2")
        assert not store.verify_password("dave", "old-Pw-1")
        assert store.verify_password("dave", "new-Pw-2")

    def test_delete(self, store):
        store.create("erin", "pw-Aa1")
        store.delete("erin")
        assert store.get("erin") is None

    def test_delete_missing_raises(self, store):
        with pytest.raises(KeyError):
            store.delete("ghost")

    def test_verify_correct_and_wrong(self, store):
        store.create("frank", "right-Pw-1")
        assert store.verify_password("frank", "right-Pw-1")
        assert not store.verify_password("frank", "wrong-Pw")

    def test_verify_unknown_user_is_false(self, store):
        # Timing-safe path: unknown user still returns False, never raises.
        assert not store.verify_password("nobody", "whatever")

    def test_external_account_never_authenticates_locally(self, store):
        # Empty password -> unusable-password sentinel ("!"), never a "$2" hash.
        store.create("ext", "")
        assert not store.verify_password("ext", "")
        assert not store.verify_password("ext", "anything")

    def test_a_refused_create_leaves_the_stored_account_as_it_was(self, store):
        store.create("bob", "pw-Aa1")
        before = store.get("bob")

        with pytest.raises(ValueError, match="Account already exists: bob"):
            store.create("bob", "other-Pw")

        assert store.get("bob") == before

    def test_the_external_stamps_round_trip(self, store):
        # The identity guard decides on these two fields, so a backend that drops
        # either reads every IdP-owned account as a local one.
        store.create("stamped", "")
        store.update("stamped", external=True, source_provider="entra")
        stored = store.get("stamped")
        assert stored.external is True
        assert stored.source_provider == "entra"


class TestReplicasCreatingOneAccountAtOnce:
    """Each replica holds its own client, so only the store can decide which create wins."""

    REPLICAS = 4

    @pytest.fixture
    def replicas(self, docu):
        database = docu.collection("accounts").database.name
        clients = [DocuStore(_URI, database) for _ in range(self.REPLICAS)]
        try:
            yield [DocuStoreAccountStore(client, collection="accounts") for client in clients]
        finally:
            for client in clients:
                client.close()

    def test_exactly_one_create_succeeds_and_its_account_is_the_one_stored(self, replicas):
        barrier = threading.Barrier(self.REPLICAS)

        def create(index: int) -> str | None:
            password = f"password-{index}-Aa1"
            barrier.wait(timeout=30)
            try:
                replicas[index].create("alice", password)
            except ValueError:
                return None
            return password

        with ThreadPoolExecutor(max_workers=self.REPLICAS) as pool:
            created = [p for p in pool.map(create, range(self.REPLICAS)) if p is not None]

        assert len(created) == 1, created
        assert replicas[0].verify_password("alice", created[0])
        assert len(replicas[0].list()) == 1


class TestJitIdentityGuardOnTheDocumentStore:
    """The identity guard and the SCIM binding, against the document backend.

    The guard reads ``external`` and ``source_provider`` off whatever the store
    returns, so it is only as good as the backend's round-trip of them. #502 and
    the binding were both proven on the YAML store alone.
    """

    def test_a_local_account_is_refused_and_left_untouched(self, store, tmp_path):
        groups = GroupStore(tmp_path / "groups")
        groups.create("dfe-admins", roles=["admin"])
        store.create("jane-corp-com", "localpass-Pw-1", groups=["dfe-admins"])
        jit = JitProvisioner(
            account_store=store,
            group_store=groups,
            source_provider_bindings={"": "entra", SCIM_SOURCE_PROVIDER: "entra"},
        )

        with pytest.raises(JitIdentityCollisionError) as refused:
            jit.ensure_account("jane@corp.com", ["dfe-admins"], "entra", email="evil@example.com")

        assert refused.value.reason == "local_account"
        stored = store.get("jane-corp-com")
        assert stored.groups == ["dfe-admins"]
        assert stored.email == ""
        assert stored.external is False
        assert stored.last_login_at == ""

    def test_an_unbound_scim_account_is_refused(self, store, tmp_path):
        groups = GroupStore(tmp_path / "groups")
        groups.create("dfe-admins", roles=["admin"])
        store.create("jane-corp-com", "provisioned-Pw-1", groups=[])
        store.update("jane-corp-com", source_provider=SCIM_SOURCE_PROVIDER)
        jit = JitProvisioner(account_store=store, group_store=groups)

        with pytest.raises(JitIdentityCollisionError) as refused:
            jit.ensure_account("jane@corp.com", ["dfe-admins"], "entra")

        assert refused.value.reason == "provider_mismatch"
        assert store.get("jane-corp-com").groups == []

    def test_the_bound_provider_reconciles_the_scim_account(self, store, tmp_path):
        groups = GroupStore(tmp_path / "groups")
        groups.create("dfe-admins", roles=["admin"])
        store.create("jane-corp-com", "provisioned-Pw-1", groups=[])
        store.update("jane-corp-com", source_provider=SCIM_SOURCE_PROVIDER)
        jit = JitProvisioner(
            account_store=store,
            group_store=groups,
            source_provider_bindings={SCIM_SOURCE_PROVIDER: "entra"},
        )

        account = jit.ensure_account(
            "jane@corp.com", ["dfe-admins"], "entra", email="jane@corp.com"
        )

        assert account.groups == ["dfe-admins"]
        assert account.email == "jane@corp.com"
        assert account.last_login_at != ""
        assert account.source_provider == SCIM_SOURCE_PROVIDER

    def test_a_second_subject_on_the_stem_is_refused(self, store, tmp_path):
        """The guard reads the recorded subject off this backend, so it must round-trip."""
        groups = GroupStore(tmp_path / "groups")
        groups.create("dfe-admins", roles=["admin"])
        jit = JitProvisioner(account_store=store, group_store=groups)
        jit.ensure_account("Jane.Doe@corp.com", [], "entra")

        with pytest.raises(JitIdentityCollisionError) as refused:
            jit.ensure_account("jane-doe@corp.com", ["dfe-admins"], "entra")

        assert refused.value.reason == "subject_mismatch"
        assert store.get("jane-doe-corp-com").groups == []

    def test_an_unbound_provider_is_still_refused(self, store, tmp_path):
        groups = GroupStore(tmp_path / "groups")
        groups.create("dfe-admins", roles=["admin"])
        store.create("jane-corp-com", "provisioned-Pw-1", groups=[])
        store.update("jane-corp-com", source_provider=SCIM_SOURCE_PROVIDER)
        jit = JitProvisioner(
            account_store=store,
            group_store=groups,
            source_provider_bindings={SCIM_SOURCE_PROVIDER: "entra"},
        )

        with pytest.raises(JitIdentityCollisionError) as refused:
            jit.ensure_account("jane@corp.com", ["dfe-admins"], "okta")

        assert refused.value.reason == "provider_mismatch"
        assert store.get("jane-corp-com").groups == []


class TestProtectedNameFloor:
    """The floor from issue #505 holds on this backend exactly as on the YAML one."""

    @pytest.fixture
    def seeded(self, store):
        store.create("admin", "s3cret-Pw", groups=[RECOVERY_GROUP])
        store.create(BREAKGLASS, "s3cret-Pw", groups=[RECOVERY_GROUP])
        return store

    @pytest.mark.parametrize("username", ["admin", BREAKGLASS])
    def test_disable_is_refused(self, seeded, username):
        with pytest.raises(ProtectedAccountError):
            seeded.update(username, enabled=False)
        assert seeded.get(username).enabled is True

    @pytest.mark.parametrize("username", ["admin", BREAKGLASS])
    def test_delete_is_refused(self, seeded, username):
        with pytest.raises(ProtectedAccountError):
            seeded.delete(username)
        assert seeded.get(username) is not None

    @pytest.mark.parametrize("username", ["admin", BREAKGLASS])
    def test_dropping_the_admin_group_is_refused(self, seeded, username):
        with pytest.raises(ProtectedAccountError):
            seeded.update(username, groups=[])
        assert seeded.get(username).groups == [RECOVERY_GROUP]

    @pytest.mark.parametrize("username", ["admin", BREAKGLASS])
    def test_put_of_a_disabled_record_is_refused(self, seeded, username):
        with pytest.raises(ProtectedAccountError):
            seeded.put(
                Account(
                    username=username,
                    password_hash="$2b$12$x",
                    enabled=False,
                    groups=[RECOVERY_GROUP],
                )
            )
        assert seeded.get(username).enabled is True

    @pytest.mark.parametrize("username", ["admin", BREAKGLASS])
    def test_password_reset_and_contact_edits_still_work(self, seeded, username):
        seeded.reset_password(username, "another-Pw-1")
        assert seeded.verify_password(username, "another-Pw-1")
        assert seeded.update(username, email="ops@example.com").email == "ops@example.com"

    @pytest.mark.parametrize("username", ["admin", BREAKGLASS])
    def test_allow_protected_reaches_past_the_floor(self, seeded, username):
        assert seeded.update(username, enabled=False, allow_protected=True).enabled is False
        seeded.delete(username, allow_protected=True)
        assert seeded.get(username) is None

    def test_a_missing_protected_name_answers_keyerror(self, store):
        with pytest.raises(KeyError):
            store.delete(BREAKGLASS)

    def test_an_unprotected_account_is_untouched(self, store):
        store.create("alice", "s3cret-Pw", groups=[RECOVERY_GROUP])
        store.update("alice", enabled=False, groups=[])
        store.delete("alice")
        assert store.get("alice") is None
