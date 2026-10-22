#  Project:      dfe-engine
#  File:         tests/integration/test_docustore_concurrency.py
#  Purpose:      Document-store writes from two replicas at once, against a real store
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Concurrent writes through two DocuStore clients, as two engine replicas make them.

Runs against the harness's document store (``mongo_uri``: ``DFE_TEST_MONGO_URI``, else
DocumentDB on docker), each test in a throwaway database dropped on teardown. Each
client holds its own connection pool, so only the store can order their writes.
DocumentDB fails the losing side of a write race with 11000 or 112, so every race here
runs enough rounds to meet both.
"""

import pytest
from pydantic import BaseModel

from dfe_engine.store.documents import DocumentConflictError
from tests.support.races import race

pytestmark = pytest.mark.integration

THREADS_PER_CLIENT = 50
INCREMENTS_PER_THREAD = 4
RACE_ROUNDS = 100
# Each round waits on DocumentDB's index-build schedule, which runs every 2 s.
INDEX_ROUNDS = 10


class Counter(BaseModel):
    """One named counter."""

    name: str
    value: int = 0


class Item(BaseModel):
    """A document carrying a second field that must be unique."""

    name: str
    token: str


def _bump(counter: Counter) -> Counter:
    return counter.model_copy(update={"value": counter.value + 1})


class TestMutate:
    """Read, apply and compare-and-swap on ``_rev``: no update is lost to a concurrent one."""

    def test_two_clients_of_fifty_threads_each_lose_no_increment(self, docustore_pair):
        counters = [c.typed("counters", Counter, key="name") for c in docustore_pair]
        assert counters[0].insert(Counter(name="hits"))

        def worker(index: int) -> int:
            store = counters[index % 2]
            for _ in range(INCREMENTS_PER_THREAD):
                store.mutate("hits", _bump, attempts=400)
            return INCREMENTS_PER_THREAD

        results = race([lambda i=i: worker(i) for i in range(2 * THREADS_PER_CLIENT)])

        errors = [r for r in results if isinstance(r, BaseException)]
        assert errors == []
        total = 2 * THREADS_PER_CLIENT * INCREMENTS_PER_THREAD
        assert counters[1].get("hits") == Counter(name="hits", value=total)

    def test_spent_attempts_raise_a_conflict_and_lose_nothing_that_succeeded(self, docustore_pair):
        counters = [c.typed("counters", Counter, key="name") for c in docustore_pair]
        assert counters[0].insert(Counter(name="hits"))

        results = race(
            [
                lambda i=i: counters[i % 2].mutate("hits", _bump, attempts=1)
                for i in range(2 * THREADS_PER_CLIENT)
            ]
        )

        conflicts = [r for r in results if isinstance(r, DocumentConflictError)]
        landed = [r for r in results if isinstance(r, Counter)]
        assert len(conflicts) + len(landed) == len(results), [
            r for r in results if not isinstance(r, (Counter, DocumentConflictError))
        ]
        assert conflicts, "no write lost a race, so the bound was never reached"
        assert counters[0].get("hits").value == len(landed)

    def test_the_function_sees_the_stored_state_and_its_result_is_stored(self, docustore_pair):
        store = docustore_pair[0].typed("counters", Counter, key="name")
        store.insert(Counter(name="hits", value=41))

        result = store.mutate("hits", _bump)

        assert result == Counter(name="hits", value=42)
        assert store.get("hits") == result

    def test_a_missing_document_raises_keyerror(self, docustore_pair):
        store = docustore_pair[0].typed("counters", Counter, key="name")
        with pytest.raises(KeyError):
            store.mutate("ghost", _bump)

    def test_a_refusal_inside_the_function_writes_nothing(self, docustore_pair):
        store = docustore_pair[0].typed("counters", Counter, key="name")
        store.insert(Counter(name="hits", value=1))
        before = docustore_pair[0].revision("counters")

        def refuse(counter: Counter) -> Counter:
            raise ValueError("refused")

        with pytest.raises(ValueError, match="refused"):
            store.mutate("hits", refuse)

        assert store.get("hits").value == 1
        assert docustore_pair[0].revision("counters") == before

    def test_the_key_cannot_be_changed(self, docustore_pair):
        store = docustore_pair[0].typed("counters", Counter, key="name")
        store.insert(Counter(name="hits"))

        with pytest.raises(ValueError, match="key"):
            store.mutate("hits", lambda c: c.model_copy(update={"name": "other"}))

        assert store.get("hits") is not None
        assert store.get("other") is None

    def test_a_swap_never_lands_on_a_document_recreated_under_it(self, docustore_pair):
        """A delete and a fresh insert between the read and the swap leave ``_rev`` at 1 again."""
        store, other = (c.typed("counters", Counter, key="name") for c in docustore_pair)
        assert store.insert(Counter(name="hits", value=10))
        seen: list[int] = []

        def recreate_then_bump(counter: Counter) -> Counter:
            seen.append(counter.value)
            if len(seen) == 1:
                assert other.delete("hits")
                assert other.insert(Counter(name="hits", value=0))
            return _bump(counter)

        result = store.mutate("hits", recreate_then_bump)

        assert seen == [10, 0]
        assert result == Counter(name="hits", value=1)
        assert other.get("hits") == Counter(name="hits", value=1)

    def test_a_document_written_without_a_revision_is_mutated(self, docustore_pair):
        """A raw write, as a hand edit leaves it, carries no ``_rev`` and still takes a CAS write."""
        docustore_pair[0].collection("counters").insert_one({"name": "hits", "value": 7})
        store = docustore_pair[0].typed("counters", Counter, key="name")

        assert store.mutate("hits", _bump).value == 8
        assert docustore_pair[0].collection("counters").find_one({"name": "hits"})["_rev"] == 1


class TestIndexes:
    """Two replicas booting at once each make a fresh collection's unique indexes."""

    def test_two_clients_indexing_one_new_collection_at_once_both_succeed(self, docustore_pair):
        for round_ in range(INDEX_ROUNDS):
            name = f"boot_{round_}"
            results = race(
                [
                    lambda docu=docu, name=name: docu.typed(
                        name, Item, key="name", unique=("token",)
                    )
                    for docu in docustore_pair
                ]
            )
            assert [r for r in results if isinstance(r, BaseException)] == [], (round_, results)


class TestRevision:
    """Every document carries ``_rev``, which reads never surface in the model."""

    def test_insert_starts_it_and_each_write_moves_it_by_one(self, docustore_pair):
        store = docustore_pair[0].typed("counters", Counter, key="name")
        raw = docustore_pair[0].collection("counters")

        store.insert(Counter(name="hits"))
        assert raw.find_one({"name": "hits"})["_rev"] == 1
        store.put("hits", Counter(name="hits", value=5))
        assert raw.find_one({"name": "hits"})["_rev"] == 2
        store.mutate("hits", _bump)
        assert raw.find_one({"name": "hits"})["_rev"] == 3

    def test_put_creates_a_document_at_the_first_revision(self, docustore_pair):
        store = docustore_pair[0].typed("counters", Counter, key="name")
        store.put("hits", Counter(name="hits"))
        assert docustore_pair[0].collection("counters").find_one({"name": "hits"})["_rev"] == 1

    def test_reads_leave_it_out(self, docustore_pair):
        store = docustore_pair[0].typed("counters", Counter, key="name")
        store.insert(Counter(name="hits", value=3))
        assert store.get("hits") == Counter(name="hits", value=3)
        assert store.list() == [Counter(name="hits", value=3)]


class TestInsert:
    """A create through two replicas at once: the unique index decides, never a crash."""

    def test_two_clients_inserting_one_key_exactly_one_wins_every_round(self, docustore_pair):
        stores = [c.typed("counters", Counter, key="name") for c in docustore_pair]

        for round_ in range(RACE_ROUNDS):
            name = f"key-{round_}"
            results = race(
                [
                    lambda name=name: stores[0].insert(Counter(name=name, value=0)),
                    lambda name=name: stores[1].insert(Counter(name=name, value=1)),
                ]
            )
            assert sorted(map(repr, results)) == ["False", "True"], (round_, results)
            winner = results.index(True)
            assert stores[0].get(name) == Counter(name=name, value=winner)


class TestSecondaryUnique:
    """A second unique field is enforced the way the key is."""

    def test_an_insert_that_reuses_the_field_is_refused(self, docustore_pair):
        store = docustore_pair[0].typed("items", Item, key="name", unique=("token",))

        assert store.insert(Item(name="first", token="t-1"))
        assert not store.insert(Item(name="second", token="t-1"))
        assert store.get("second") is None

    def test_two_clients_reusing_the_field_at_once_store_one(self, docustore_pair):
        stores = [c.typed("items", Item, key="name", unique=("token",)) for c in docustore_pair]

        for round_ in range(RACE_ROUNDS):
            token = f"t-{round_}"
            results = race(
                [
                    lambda r=round_, t=token: stores[0].insert(Item(name=f"a-{r}", token=t)),
                    lambda r=round_, t=token: stores[1].insert(Item(name=f"b-{r}", token=t)),
                ]
            )
            assert sorted(map(repr, results)) == ["False", "True"], (round_, results)
        assert len(stores[0].list()) == RACE_ROUNDS


class TestClaims:
    """Insert-if-absent on a unique key: of two claimers exactly one holds it."""

    def test_two_clients_claiming_one_key_exactly_one_wins_every_round(self, docustore_pair):
        claims = [c.claims("tenant_keys") for c in docustore_pair]

        for round_ in range(RACE_ROUNDS):
            key = f"tenant-{round_}"
            results = race(
                [
                    lambda key=key: claims[0].claim(key, "acme"),
                    lambda key=key: claims[1].claim(key, "globex"),
                ]
            )
            assert sorted(map(repr, results)) == ["False", "True"], (round_, results)
            assert claims[0].holder(key) == ("acme" if results[0] else "globex")

    def test_the_holder_claiming_again_still_holds_it(self, docustore_pair):
        claims = docustore_pair[0].claims("tenant_keys")
        assert claims.claim("tenant-1", "acme")
        assert claims.claim("tenant-1", "acme")
        assert not claims.claim("tenant-1", "globex")

    def test_only_the_holder_releases_it(self, docustore_pair):
        claims = docustore_pair[0].claims("tenant_keys")
        claims.claim("tenant-1", "acme")

        assert not claims.release("tenant-1", "globex")
        assert claims.holder("tenant-1") == "acme"
        assert claims.release("tenant-1", "acme")
        assert claims.holder("tenant-1") is None

    def test_a_released_key_is_claimed_again(self, docustore_pair):
        claims = docustore_pair[0].claims("tenant_keys")
        claims.claim("tenant-1", "acme")
        claims.release("tenant-1", "acme")
        assert claims.claim("tenant-1", "globex")
        assert claims.holder("tenant-1") == "globex"

    def test_the_holder_claiming_again_stamps_it_fresh(self, docustore_pair):
        claims = docustore_pair[0].claims("tenant_keys")
        claims.claim("tenant-1", "acme")
        raw = docustore_pair[0].collection("tenant_keys")
        raw.update_one({"key": "tenant-1"}, {"$set": {"claimed_at": "2020-01-01T00:00:00+00:00"}})

        claims.claim("tenant-1", "acme")

        assert raw.find_one({"key": "tenant-1"})["claimed_at"] > "2026"

    def test_claims_are_listed_by_subject_and_owner(self, docustore_pair):
        claims = docustore_pair[0].claims("tenant_keys")
        claims.claim("a/1", "acme", subject="a")
        claims.claim("a/2", "globex", subject="a")
        claims.claim("b/1", "acme", subject="b")

        assert [c.key for c in claims.about("a")] == ["a/1", "a/2"]
        assert [c.key for c in claims.owned_by("acme")] == ["a/1", "b/1"]


class TestOrphanedClaims:
    """A claim nothing holds is freed once it is older than the grace, never sooner."""

    def test_an_old_claim_nothing_holds_is_released(self, docustore_pair):
        claims = docustore_pair[0].claims("tenant_keys")
        claims.claim("tenant-1", "acme")
        claim = claims.about("")[0]

        assert claims.release_if_orphaned(claim, held=False, grace_seconds=0)
        assert claims.holder("tenant-1") is None

    def test_a_held_or_fresh_claim_is_kept(self, docustore_pair):
        claims = docustore_pair[0].claims("tenant_keys")
        claims.claim("tenant-1", "acme")
        claim = claims.about("")[0]

        assert not claims.release_if_orphaned(claim, held=True, grace_seconds=0)
        assert not claims.release_if_orphaned(claim, held=False, grace_seconds=3600)
        assert claims.holder("tenant-1") == "acme"

    def test_a_claim_renewed_since_it_was_read_is_kept(self, docustore_pair):
        claims = docustore_pair[0].claims("tenant_keys")
        claims.claim("tenant-1", "acme")
        stale = claims.about("")[0].model_copy(update={"claimed_at": "2020-01-01T00:00:00+00:00"})

        assert not claims.release_if_orphaned(stale, held=False, grace_seconds=0)
        assert claims.holder("tenant-1") == "acme"

    def test_a_claim_released_and_made_again_at_the_same_stamp_is_kept(self, docustore_pair):
        """Two replicas' clocks can stamp two claims alike, so the stamp alone names neither."""
        claims = docustore_pair[0].claims("tenant_keys")
        raw = docustore_pair[0].collection("tenant_keys")
        claims.claim("tenant-1", "acme")
        read = claims.about("")[0]
        assert claims.release("tenant-1", "acme")
        assert docustore_pair[1].claims("tenant_keys").claim("tenant-1", "acme")
        raw.update_one({"key": "tenant-1"}, {"$set": {"claimed_at": read.claimed_at}})

        assert not claims.release_if_orphaned(read, held=False, grace_seconds=0)
        assert claims.holder("tenant-1") == "acme"


class TestStoreRevisions:
    """One counter per collection moves once for every write and never for anything else."""

    def test_each_write_moves_its_collection_once(self, docustore_pair):
        docu = docustore_pair[0]
        store = docu.typed("counters", Counter, key="name")
        other = docu.typed("items", Item, key="name")
        assert docu.revision("counters") == 0

        store.insert(Counter(name="hits"))
        assert docu.revision("counters") == 1
        store.put("hits", Counter(name="hits", value=2))
        assert docu.revision("counters") == 2
        store.mutate("hits", _bump)
        assert docu.revision("counters") == 3
        store.delete("hits")
        assert docu.revision("counters") == 4
        assert docu.revision("items") == 0

        other.insert(Item(name="a", token="t"))
        assert (docu.revision("counters"), docu.revision("items")) == (4, 1)

    def test_a_write_that_does_not_happen_leaves_it(self, docustore_pair):
        docu = docustore_pair[0]
        store = docu.typed("counters", Counter, key="name")
        store.insert(Counter(name="hits", value=1))
        before = docu.revision("counters")

        assert not store.insert(Counter(name="hits"))
        assert not store.delete("ghost")
        assert store.mutate("hits", lambda c: c) == Counter(name="hits", value=1)

        assert docu.revision("counters") == before

    def test_claims_move_their_collection(self, docustore_pair):
        docu = docustore_pair[0]
        claims = docu.claims("tenant_keys")

        claims.claim("tenant-1", "acme")
        claims.claim("tenant-1", "acme")
        claims.release("tenant-1", "acme")

        # The insert, the holder's renewal, and the release.
        assert docu.revision("tenant_keys") == 3

    def test_two_clients_of_fifty_threads_each_count_every_write(self, docustore_pair):
        stores = [c.typed("counters", Counter, key="name") for c in docustore_pair]

        def worker(index: int) -> int:
            for step in range(INCREMENTS_PER_THREAD):
                stores[index % 2].put(f"w{index}", Counter(name=f"w{index}", value=step))
            return INCREMENTS_PER_THREAD

        results = race([lambda i=i: worker(i) for i in range(2 * THREADS_PER_CLIENT)])

        assert [r for r in results if isinstance(r, BaseException)] == []
        total = 2 * THREADS_PER_CLIENT * INCREMENTS_PER_THREAD
        assert docustore_pair[1].revision("counters") == total
