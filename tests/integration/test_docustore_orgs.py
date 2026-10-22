#  Project:      dfe-engine
#  File:         tests/integration/test_docustore_orgs.py
#  Purpose:      Orgs, tenant id claims and ingest credentials in the document store, from two replicas
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The document-store org registry, written by two replicas at once.

Runs against the harness's document store (``mongo_uri``), each test in a throwaway
database. Each replica holds its own client, so only the store orders their writes.
"""

import functools
import threading
from pathlib import Path
from typing import override

import pytest

from dfe_engine.orgs.ingest_credentials import (
    CredentialChangedError,
    CredentialIdTakenError,
    IngestCredential,
    OrgUnavailableError,
)
from dfe_engine.orgs.ingest_tokens import (
    bearer_entries,
    bearer_secret_path,
    drop_token,
    store_token,
    sync_bearer_tokens,
)
from dfe_engine.orgs.models import Org, ReservedOrgError
from dfe_engine.orgs.registry import (
    BINDINGS_COLLECTION,
    CLAIM_GRACE_SECONDS,
    DELETES_COLLECTION,
    TENANT_KEYS_COLLECTION,
    DocuStoreOrgRegistry,
    OrgBoundError,
    OrgCollisionError,
    OrgExistsError,
)
from dfe_engine.orgs.seed import ensure_default_org, seed_orgs
from dfe_engine.secrets import DfeSecrets, build_secrets
from dfe_engine.settings import SecretsSettings, SeedOrg
from dfe_engine.store.documents import Claim, DocumentConflictError, DocuStore, converge
from tests.support.races import race, raised

pytestmark = pytest.mark.integration

RACE_ROUNDS = 30
LONG_AGO = "2020-01-01T00:00:00+00:00"


def _registries(
    pair: tuple[DocuStore, DocuStore], *, claim_grace_seconds: float = CLAIM_GRACE_SECONDS
) -> list[DocuStoreOrgRegistry]:
    return [DocuStoreOrgRegistry(store, claim_grace_seconds=claim_grace_seconds) for store in pair]


def _org(registry: DocuStoreOrgRegistry, name: str) -> Org:
    org = registry.get(name)
    assert org is not None, name
    return org


def _claims_match_records(registry: DocuStoreOrgRegistry, store: DocuStore) -> None:
    """Every org's name and tenant ids are claimed by that org."""
    claims = store.claims(TENANT_KEYS_COLLECTION)
    for org in registry.list():
        for marker in {org.name, *(org.org_ids or [org.name])}:
            assert claims.holder(marker) == org.name, (org.name, marker)


def _orphan(store: DocuStore, key: str, owner: str = "ghost") -> None:
    """A claim a replica made and died before writing the record that holds it."""
    store.typed(TENANT_KEYS_COLLECTION, Claim, key="key").insert(
        Claim(key=key, owner=owner, claimed_at=LONG_AGO)
    )


class TestCreate:
    """A name or tenant id is held by one org, whichever replica wrote it."""

    def test_two_replicas_creating_orgs_that_share_a_tenant_id_create_one(self, docustore_pair):
        registries = _registries(docustore_pair)

        for round_ in range(RACE_ROUNDS):
            shared = f"shared-{round_}"
            results = race(
                [
                    functools.partial(registries[0].create, f"alpha-{round_}", [shared]),
                    functools.partial(registries[1].create, f"beta-{round_}", [shared]),
                ]
            )

            refused = raised(results)
            assert len(refused) == 1, (round_, results)
            assert isinstance(refused[0], OrgCollisionError), refused
            assert len([o for o in registries[1].list() if shared in o.org_ids]) == 1
        _claims_match_records(registries[0], docustore_pair[0])

    def test_two_replicas_creating_one_name_create_it_once(self, docustore_pair):
        registries = _registries(docustore_pair)

        for round_ in range(RACE_ROUNDS):
            name = f"acme-{round_}"
            results = race(
                [
                    functools.partial(registries[0].create, name, [f"{name}-a"]),
                    functools.partial(registries[1].create, name, [f"{name}-b"]),
                ]
            )

            refused = raised(results)
            assert len(refused) == 1, (round_, results)
            assert isinstance(refused[0], OrgExistsError), refused
        _claims_match_records(registries[0], docustore_pair[0])
        # The loser took back the tenant id it claimed.
        won = _org(registries[0], "acme-0").org_ids
        lost = ["acme-0-b"] if won == ["acme-0-a"] else ["acme-0-a"]
        assert registries[0].create("other", lost).org_ids == lost

    def test_a_name_another_org_uses_as_a_tenant_id_collides(self, docustore_pair):
        registry = _registries(docustore_pair)[0]
        registry.create("acme")

        with pytest.raises(OrgCollisionError, match="tenant-fenced org 'acme'"):
            registry.create("beta", ["acme"])

    def test_a_refused_create_takes_back_the_claims_it_made(self, docustore_pair):
        registry = _registries(docustore_pair)[0]
        registry.create("acme")

        with pytest.raises(OrgCollisionError):
            registry.create("beta", ["free", "acme"])

        assert registry.create("gamma", ["free"]).org_ids == ["free"]
        assert registry.get("beta") is None

    def test_an_old_claim_no_org_holds_gives_way(self, docustore_pair):
        registry = _registries(docustore_pair)[0]
        _orphan(docustore_pair[0], "t-1")

        assert registry.create("acme", ["t-1"]).org_ids == ["t-1"]
        _claims_match_records(registry, docustore_pair[0])

    def test_a_fresh_claim_no_org_holds_yet_still_collides(self, docustore_pair):
        registry = _registries(docustore_pair)[0]
        docustore_pair[0].claims(TENANT_KEYS_COLLECTION).claim("t-1", "in-flight")

        with pytest.raises(OrgCollisionError, match="'in-flight'"):
            registry.create("acme", ["t-1"])

    def test_a_stored_org_whose_claims_are_gone_still_collides(self, docustore_pair):
        registries = _registries(docustore_pair)
        registries[0].create("acme", ["a-1"])
        docustore_pair[0].collection(TENANT_KEYS_COLLECTION).delete_many({})

        with pytest.raises(OrgCollisionError, match="tenant-fenced org 'acme'"):
            registries[1].create("beta", ["a-1"])


@pytest.fixture
def lost_orgs_bump(docustore_pair, monkeypatch) -> None:
    """Replica 0's orgs revision bump raises after the record write lands, as a store blip does."""
    revisions = docustore_pair[0]._revisions
    real = revisions.bump

    def bump(name: str) -> None:
        if name == "orgs":
            raise DocumentConflictError("the orgs revision lost to concurrent writers 5 times")
        real(name)

    monkeypatch.setattr(revisions, "bump", bump)


class TestAWriteThatLandsAndThenRaises:
    """A failure after the org record is in leaves its tenant ids claimed, so no other org takes one."""

    @staticmethod
    def _fenced(registries: list[DocuStoreOrgRegistry], store: DocuStore, tenant_id: str) -> None:
        _claims_match_records(registries[1], store)
        with pytest.raises(OrgCollisionError):
            registries[1].create("intruder", [tenant_id])

    def test_a_create(self, docustore_pair, lost_orgs_bump):
        registries = _registries(docustore_pair)

        with pytest.raises(DocumentConflictError):
            registries[0].create("acme", ["a-1"])

        assert _org(registries[1], "acme").org_ids == ["a-1"]
        self._fenced(registries, docustore_pair[1], "a-1")

    def test_a_create_held_across_its_hunt_write(self, docustore_pair, lost_orgs_bump):
        registries = _registries(docustore_pair)

        with pytest.raises(DocumentConflictError):
            with registries[0].reserved("acme", ["a-1"]):
                registries[0].create("acme", ["a-1"])

        assert _org(registries[1], "acme").org_ids == ["a-1"]
        self._fenced(registries, docustore_pair[1], "a-1")

    def test_a_held_create_whose_next_step_fails(self, docustore_pair):
        registries = _registries(docustore_pair)

        with pytest.raises(RuntimeError):
            with registries[0].reserved("acme", ["a-1"]):
                registries[0].create("acme", ["a-1"])
                raise RuntimeError("the HyperDX team write failed")

        self._fenced(registries, docustore_pair[1], "a-1")

    def test_an_update(self, docustore_pair, lost_orgs_bump):
        registries = _registries(docustore_pair)
        registries[1].create("acme", ["a-1"])

        with pytest.raises(DocumentConflictError):
            registries[0].update("acme", org_ids=["a-1", "a-2"])

        assert _org(registries[1], "acme").org_ids == ["a-1", "a-2"]
        self._fenced(registries, docustore_pair[1], "a-2")


class TestUpdate:
    """An update claims what it adds and gives up what it drops."""

    def test_a_tenant_id_another_org_holds_is_refused_and_nothing_changes(self, docustore_pair):
        registry = _registries(docustore_pair)[0]
        registry.create("acme", ["a-1"])
        registry.create("beta", ["b-1"])

        with pytest.raises(OrgCollisionError):
            registry.update("acme", org_ids=["a-1", "a-2", "b-1"])

        assert _org(registry, "acme").org_ids == ["a-1"]
        assert registry.create("gamma", ["a-2"]).org_ids == ["a-2"]

    def test_a_dropped_tenant_id_past_the_grace_goes_to_the_next_org(self, docustore_pair):
        registry = _registries(docustore_pair, claim_grace_seconds=0)[0]
        registry.create("acme", ["a-1", "a-2"])

        registry.update("acme", org_ids=["a-1"])

        assert registry.create("beta", ["a-2"]).org_ids == ["a-2"]
        _claims_match_records(registry, docustore_pair[0])

    def test_a_dropped_tenant_id_within_the_grace_is_still_held(self, docustore_pair):
        registry = _registries(docustore_pair)[0]
        registry.create("acme", ["a-1", "a-2"])

        registry.update("acme", org_ids=["a-1"])

        with pytest.raises(OrgCollisionError):
            registry.create("beta", ["a-2"])

    def test_two_replicas_changing_one_orgs_tenant_ids_leave_them_all_claimed(self, docustore_pair):
        registries = _registries(docustore_pair, claim_grace_seconds=0)
        registries[0].create("acme", ["base"])

        for round_ in range(RACE_ROUNDS):
            results = race(
                [
                    functools.partial(registries[0].update, "acme", org_ids=[f"x-{round_}"]),
                    functools.partial(registries[1].update, "acme", org_ids=[f"y-{round_}"]),
                ]
            )
            assert raised(results) == [], (round_, results)
            _claims_match_records(registries[0], docustore_pair[0])

    def test_the_default_org_keeps_its_tenant_id(self, docustore_pair):
        registry = _registries(docustore_pair)[0]
        ensure_default_org(registry)

        with pytest.raises(ReservedOrgError):
            registry.update("default", org_ids=["other"])

        assert _org(registry, "default").org_ids == ["default"]
        assert registry.create("acme", ["other"]).org_ids == ["other"]

    def test_a_missing_org_is_a_keyerror(self, docustore_pair):
        with pytest.raises(KeyError):
            _registries(docustore_pair)[0].update("ghost", display_name="x")


class TestDelete:
    """A delete takes the org's credentials, and gives up its claims past the grace."""

    def test_its_tenant_ids_go_to_the_next_org(self, docustore_pair):
        registry = _registries(docustore_pair, claim_grace_seconds=0)[0]
        registry.create("acme", ["a-1"])

        registry.delete("acme")

        assert registry.create("beta", ["a-1", "acme"]).org_ids == ["a-1", "acme"]

    def test_its_credentials_go_with_it(self, docustore_pair):
        registry = _registries(docustore_pair)[0]
        registry.create("acme")
        registry.ingest_credentials.put(_credential("agents"))

        registry.delete("acme")

        assert registry.ingest_credentials.list() == []


class TestBoot:
    """Claims a dead replica left, and orgs every replica seeds at once."""

    def test_the_sweep_releases_old_claims_no_org_holds(self, docustore_pair):
        registry = _registries(docustore_pair)[0]
        registry.create("acme", ["a-1"])
        _orphan(docustore_pair[0], "t-dead")

        assert registry.release_orphaned_claims() == ["t-dead"]
        _claims_match_records(registry, docustore_pair[0])

    def test_two_replicas_ensuring_the_default_org_at_once_both_return(self, docustore_pair):
        for round_ in range(RACE_ROUNDS // 3):
            pair = [DocuStoreOrgRegistry(s, collection=f"orgs_{round_}") for s in docustore_pair]
            results = race([functools.partial(ensure_default_org, r) for r in pair])

            assert raised(results) == [], (round_, results)
            assert [o.name for o in pair[1].list()] == ["default"]
            docustore_pair[0].collection(TENANT_KEYS_COLLECTION).delete_many({})

    def test_two_replicas_seeding_at_once_both_return_with_each_org_once(self, docustore_pair):
        registries = _registries(docustore_pair)
        seeds = [SeedOrg(name=f"seed-{n}", org_ids=[f"s-{n}"]) for n in range(5)]

        results = race([functools.partial(seed_orgs, registry=r, seeds=seeds) for r in registries])

        assert raised(results) == []
        created = [name for made in results if isinstance(made, list) for name in made]
        assert sorted(created) == [s.name for s in seeds]
        assert [o.name for o in registries[1].list()] == [s.name for s in seeds]


def _credential(
    name: str,
    *,
    org: str = "acme",
    listeners: tuple[str, ...] = ("http",),
    binds: tuple[str, ...] = (),
) -> IngestCredential:
    return IngestCredential.model_validate(
        {
            "name": name,
            "org": org,
            "orgs": [org, *binds],
            "default_org": org,
            "listeners": list(listeners),
        }
    )


class TestCredentials:
    """A credential id is created once and replaced only from the state read."""

    @pytest.fixture
    def credentials(self, docustore_pair):
        registries = _registries(docustore_pair)
        registries[0].create("acme")
        registries[0].create("beta")
        return [r.ingest_credentials for r in registries]

    def test_two_replicas_creating_one_id_create_it_once(self, credentials):
        for round_ in range(RACE_ROUNDS):
            name = f"agents-{round_}"
            results = race(
                [functools.partial(c.put, _credential(name), new=True) for c in credentials]
            )

            refused = raised(results)
            assert len(refused) == 1, (round_, results)
            assert isinstance(refused[0], CredentialChangedError), refused

    def test_an_id_another_org_holds_is_taken(self, credentials):
        credentials[0].put(_credential("agents"), new=True)

        with pytest.raises(CredentialIdTakenError):
            credentials[1].put(_credential("agents", org="beta"), new=True)
        with pytest.raises(CredentialIdTakenError):
            credentials[1].put(_credential("agents", org="beta"))

    def test_a_replace_from_a_stale_read_is_refused(self, credentials):
        credentials[0].put(_credential("agents"), new=True)
        read = credentials[0].get("agents")
        credentials[1].put(_credential("agents", listeners=("otlp",)), current=read)

        with pytest.raises(CredentialChangedError):
            credentials[0].put(_credential("agents", listeners=("grpc",)), current=read)

        assert credentials[0].get("agents").listeners == ["otlp"]

    def test_two_replicas_replacing_from_one_read_land_one(self, credentials):
        credentials[0].put(_credential("agents"), new=True)

        for round_ in range(RACE_ROUNDS):
            read = credentials[0].get("agents")
            # Neither write matches the last round's, so each is a change that must land.
            pair = ("otlp", "grpc") if round_ % 2 else ("splunk_hec", "prometheus_rw")
            results = race(
                [
                    functools.partial(
                        c.put, _credential("agents", listeners=(listener,)), current=read
                    )
                    for c, listener in zip(credentials, pair, strict=True)
                ]
            )

            assert len(raised(results)) == 1, (round_, results)

    def test_a_delete_from_a_stale_read_is_refused(self, credentials):
        credentials[0].put(_credential("agents"), new=True)
        read = credentials[0].get("agents")
        credentials[1].put(_credential("agents", listeners=("otlp",)), current=read)

        with pytest.raises(CredentialChangedError):
            credentials[0].delete("agents", current=read)

        assert credentials[0].get("agents") is not None

    def test_a_delete_of_a_missing_id_is_a_keyerror(self, credentials):
        with pytest.raises(KeyError):
            credentials[0].delete("ghost")


def _orphan_in(store: DocuStore, collection: str, key: str, owner: str, subject: str = "") -> None:
    """A claim a replica made and died before finishing the write it was for."""
    store.typed(collection, Claim, key="key").insert(
        Claim(key=key, owner=owner, claimed_at=LONG_AGO, subject=subject)
    )


class TestBindings:
    """A credential write binding an org and a delete of that org never both land."""

    @pytest.fixture
    def registries(self, docustore_pair):
        registries = _registries(docustore_pair)
        registries[0].create("acme")
        registries[0].create("beta")
        return registries

    def test_a_delete_while_a_binding_write_is_in_flight_is_refused(self, registries, monkeypatch):
        credentials = registries[0].ingest_credentials
        real = credentials._c.insert
        refused = []

        # Between the binding claim and the record write, the other replica deletes beta.
        def insert(model):
            monkeypatch.setattr(credentials._c, "insert", real)
            with pytest.raises(OrgBoundError) as caught:
                registries[1].delete("beta")
            refused.append(caught.value)
            return real(model)

        monkeypatch.setattr(credentials._c, "insert", insert)

        credentials.put(_credential("agents", binds=("beta",)), new=True)

        assert refused
        assert registries[1].get("beta") is not None

    def test_a_binding_write_while_the_org_is_being_deleted_is_refused(
        self, registries, docustore_pair, monkeypatch
    ):
        credentials = registries[0].ingest_credentials
        real = credentials.owned_by

        # Inside the delete, after its checks, the other replica binds beta.
        def owned_by(org):
            monkeypatch.setattr(credentials, "owned_by", real)
            with pytest.raises(OrgUnavailableError):
                registries[1].ingest_credentials.put(
                    _credential("agents", binds=("beta",)), new=True
                )
            return real(org)

        monkeypatch.setattr(credentials, "owned_by", owned_by)

        registries[0].delete("beta")

        assert registries[1].get("beta") is None
        assert registries[1].ingest_credentials.get("agents") is None
        assert _claimed(docustore_pair[0], BINDINGS_COLLECTION) == []

    def test_a_bind_the_mark_refuses_never_stops_the_delete_holding_it(
        self, registries, docustore_pair
    ):
        with registries[0].deleting("beta"):
            # A bind that claimed after the mark went up and has not yet seen it and withdrawn.
            docustore_pair[1].claims(BINDINGS_COLLECTION).create(
                "beta/agents", "agents", subject="beta"
            )
            registries[0].delete("beta")

        assert registries[1].get("beta") is None

    def test_a_deleted_credential_frees_the_orgs_it_bound_at_once(self, registries):
        credentials = registries[0].ingest_credentials
        credentials.put(_credential("agents", binds=("beta",)), new=True)

        credentials.delete("agents")
        registries[1].delete("beta")

        assert registries[1].get("beta") is None

    def test_an_org_an_update_unbinds_is_held_for_the_grace(self, docustore_pair):
        for grace, kept in ((CLAIM_GRACE_SECONDS, True), (0, False)):
            registries = _registries(docustore_pair, claim_grace_seconds=grace)
            for name in ("acme", "beta"):
                if registries[0].get(name) is None:
                    registries[0].create(name)
            credentials = registries[0].ingest_credentials
            credentials.put(_credential("agents", binds=("beta",)))
            credentials.put(_credential("agents"))

            if kept:
                with pytest.raises(OrgBoundError, match="'agents'"):
                    registries[1].delete("beta")
            else:
                registries[1].delete("beta")
                assert registries[1].get("beta") is None

    def test_an_old_binding_no_credential_holds_gives_way(self, registries, docustore_pair):
        _orphan_in(docustore_pair[0], BINDINGS_COLLECTION, "beta/ghost", "ghost", "beta")

        registries[1].delete("beta")

        assert registries[1].get("beta") is None

    def test_an_old_delete_mark_does_not_stop_a_binding(self, registries, docustore_pair):
        _orphan_in(docustore_pair[0], DELETES_COLLECTION, "beta", "beta")

        registries[0].ingest_credentials.put(_credential("agents", binds=("beta",)), new=True)

        assert registries[1].ingest_credentials.get("agents") is not None

    def test_a_binding_an_org_that_is_gone_is_refused(self, registries, docustore_pair):
        with pytest.raises(OrgUnavailableError, match="'gamma'"):
            registries[0].ingest_credentials.put(_credential("agents", binds=("gamma",)), new=True)

        assert _claimed(docustore_pair[0], BINDINGS_COLLECTION) == []


def _owners_all_exist(registry: DocuStoreOrgRegistry) -> None:
    """Every stored credential's owning org is a stored org."""
    orgs = {org.name for org in registry.list()}
    credentials = registry.ingest_credentials.list()
    assert all(c.org in orgs for c in credentials), credentials


class TestAnOrgsOwnCredentials:
    """A credential written for an org while another replica deletes it never outlives the org."""

    @pytest.fixture
    def registries(self, docustore_pair):
        registries = _registries(docustore_pair)
        registries[0].create("beta")
        return registries

    def test_one_created_after_the_delete_listed_them_is_refused(self, registries, monkeypatch):
        credentials = registries[0].ingest_credentials
        real = credentials.owned_by
        refused = []

        # The delete has listed beta's credentials when the other replica creates one for beta.
        def owned_by(org):
            listed = real(org)
            monkeypatch.setattr(credentials, "owned_by", real)
            with pytest.raises(OrgUnavailableError) as caught:
                registries[1].ingest_credentials.put(_credential("late", org="beta"), new=True)
            refused.append(caught.value)
            return listed

        monkeypatch.setattr(credentials, "owned_by", owned_by)

        registries[0].delete("beta")

        assert refused
        assert registries[1].get("beta") is None
        _owners_all_exist(registries[1])

    def test_one_in_flight_when_the_delete_starts_holds_it_off(self, registries, monkeypatch):
        credentials = registries[1].ingest_credentials
        real = credentials._c.insert
        refused = []

        # Between the create's claims and its record write, the other replica deletes beta.
        def insert(model):
            monkeypatch.setattr(credentials._c, "insert", real)
            with pytest.raises(OrgBoundError, match="'agents'") as caught:
                registries[0].delete("beta")
            refused.append(caught.value)
            return real(model)

        monkeypatch.setattr(credentials._c, "insert", insert)

        credentials.put(_credential("agents", org="beta"), new=True)

        assert refused
        assert registries[0].get("beta") is not None
        _owners_all_exist(registries[0])

    def test_a_delete_still_takes_the_ones_it_finds(self, registries):
        registries[1].ingest_credentials.put(_credential("agents", org="beta"), new=True)

        registries[0].delete("beta")

        assert registries[1].ingest_credentials.list() == []


def _claimed(store: DocuStore, collection: str) -> list[str]:
    """The keys of every claim in *collection*."""
    return sorted(doc["key"] for doc in store.collection(collection).find())


class TestReservedCreates:
    """A create held across its hunt write is refused before it has written anything."""

    def test_a_second_create_held_for_one_name_is_refused(self, docustore_pair):
        registries = _registries(docustore_pair)

        with registries[0].reserved("acme", []):
            with pytest.raises(OrgExistsError, match="being created"):
                with registries[1].reserved("acme", ["acme-2"]):
                    pytest.fail("a second create of one name was held")

    def test_a_collision_is_refused_before_the_block_runs(self, docustore_pair):
        registries = _registries(docustore_pair)
        registries[0].create("alpha", ["shared"])

        with pytest.raises(OrgCollisionError):
            with registries[1].reserved("beta", ["shared"]):
                pytest.fail("a colliding create was held")

    def test_a_failure_inside_takes_back_what_it_held(self, docustore_pair):
        registries = _registries(docustore_pair)

        with pytest.raises(RuntimeError):
            with registries[0].reserved("beta", ["shared"]):
                raise RuntimeError("the hunt write failed")

        assert registries[1].create("gamma", ["shared"]).org_ids == ["shared"]
        with registries[1].reserved("beta", []):
            pass

    def test_a_create_inside_keeps_what_it_held(self, docustore_pair):
        registries = _registries(docustore_pair)

        with registries[0].reserved("beta", ["shared"]):
            registries[0].create("beta", ["shared"])

        _claims_match_records(registries[0], docustore_pair[0])
        with pytest.raises(OrgCollisionError):
            registries[1].create("gamma", ["shared"])


class _Paused(DfeSecrets):
    """A secrets seam whose first write waits until ``release`` is set, after setting ``reached``."""

    def __init__(self, inner: DfeSecrets) -> None:
        self._inner = inner
        self.reached = threading.Event()
        self.release = threading.Event()

    @override
    def get_or_none(self, path: str) -> str | None:
        return self._inner.get_or_none(path)

    @override
    def put(self, path: str, value: str) -> None:
        if not self.reached.is_set():
            self.reached.set()
            self.release.wait(timeout=30)
        self._inner.put(path, value)


class TestBearerConvergence:
    """A bearer sync that read the credentials before another replica changed them corrects itself."""

    @pytest.fixture
    def secrets(self, tmp_path: Path) -> DfeSecrets:
        return build_secrets(SecretsSettings(provider="file", path=str(tmp_path / "secrets")))

    def test_a_stale_pass_is_rewritten_by_the_next(self, docustore_pair, secrets):
        registries = _registries(docustore_pair)
        registries[0].create("acme")
        for name in ("doomed", "kept"):
            store_token(secrets, name, f"token-{name}")
            registries[0].ingest_credentials.put(_credential(name), new=True)
        paused = _Paused(secrets)

        # Replica 0 reads both credentials, then waits to write until replica 1 has finished.
        stale = threading.Thread(
            target=converge, args=(functools.partial(sync_bearer_tokens, registries[0], paused),)
        )
        stale.start()
        assert paused.reached.wait(timeout=30)
        registries[1].ingest_credentials.delete("doomed")
        drop_token(secrets, "doomed")
        converge(functools.partial(sync_bearer_tokens, registries[1], secrets))
        paused.release.set()
        stale.join(timeout=30)

        expected = bearer_entries(registries[1].ingest_credentials.list(), secrets)
        for listener, content in expected.items():
            assert secrets.get_or_none(bearer_secret_path(listener)) == content, listener
        assert "doomed" not in (secrets.get_or_none(bearer_secret_path("http")) or "")


def test_an_org_reads_back_as_it_was_written(docustore_pair):
    registry = _registries(docustore_pair)[0]
    made = registry.create("acme", ["a-1"], display_name="Acme")

    assert registry.get("acme") == made
    assert isinstance(made, Org)
    assert registry.update("acme", display_name="Acme Corp").display_name == "Acme Corp"
