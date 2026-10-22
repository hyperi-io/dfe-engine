#  Project:      dfe-engine
#  File:         tests/integration/test_docustore_oidc_registry.py
#  Purpose:      The document-store OIDC provider registry written through two replicas at once
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""OIDC provider writes through two replicas' clients at once, against a real store.

Runs against the harness's document store (``mongo_uri``: ``DFE_TEST_MONGO_URI``, else
DocumentDB on docker). Each race runs many rounds, because a lost update needs the two
writes to overlap and one round rarely shows it.
"""

import pytest

from dfe_engine.auth.oidc.models import OIDCProvider
from dfe_engine.auth.oidc.registry import PROVIDERS_COLLECTION, DocuStoreOIDCProviderRegistry
from dfe_engine.store.documents import DocuStore
from tests.support.races import race, raised

pytestmark = pytest.mark.integration

ROUNDS = 40


def _registries(pair: tuple[DocuStore, DocuStore]) -> list[DocuStoreOIDCProviderRegistry]:
    return [DocuStoreOIDCProviderRegistry(store) for store in pair]


def test_two_replicas_creating_one_provider_exactly_one_wins(docustore_pair):
    first, second = _registries(docustore_pair)

    for round_ in range(ROUNDS):
        name = f"idp-{round_}"
        results = race(
            [
                lambda name=name: first.create(name, OIDCProvider(display_name="first")),
                lambda name=name: second.create(name, OIDCProvider(display_name="second")),
            ]
        )

        refused = [r for r in results if isinstance(r, ValueError)]
        assert len(refused) == 1, (round_, results)
        winner = next(r for r in results if not isinstance(r, BaseException))
        assert first.get(name) == winner, round_
    assert len(first.list()) == ROUNDS


def test_a_disable_and_a_sync_stamp_at_once_keep_both(docustore_pair):
    first, second = _registries(docustore_pair)

    for round_ in range(ROUNDS):
        name = f"idp-{round_}"
        first.create(name, OIDCProvider())
        stamp = f"2026-10-22T00:00:{round_:02d}+00:00"

        results = race(
            [
                lambda name=name: first.update(name, enabled=False),
                lambda name=name, stamp=stamp: second.update(
                    name, last_sync_at=stamp, last_sync_status="ok"
                ),
            ]
        )

        assert raised(results) == []
        stored = first.get(name)
        assert stored is not None
        assert (stored.enabled, stored.last_sync_at) == (False, stamp), round_


def test_two_seeds_of_one_provider_at_once_leave_one_document(docustore_pair):
    first, second = _registries(docustore_pair)

    for round_ in range(ROUNDS):
        name = f"idp-{round_}"
        results = race(
            [
                lambda name=name: first.put(name, OIDCProvider(display_name="seeded")),
                lambda name=name: second.put(name, OIDCProvider(display_name="seeded")),
            ]
        )

        assert raised(results) == [], round_
    raw = docustore_pair[0].collection(PROVIDERS_COLLECTION)
    assert raw.count_documents({}) == ROUNDS


def test_an_update_that_changes_no_field_still_moves_the_counter(docustore_pair):
    """A secret rotated at the same path changes no field, and every relying party must rebuild."""
    first, second = _registries(docustore_pair)
    first.create("acme", OIDCProvider(client_secret_path="oidc/acme/client_secret"))
    before = second.revision()

    first.update("acme", client_secret_path="oidc/acme/client_secret")

    assert second.revision() == before + 1


def test_a_stored_document_that_does_not_load_is_left_out(docustore_pair):
    first, second = _registries(docustore_pair)
    first.create("good", OIDCProvider())
    docustore_pair[0].collection(PROVIDERS_COLLECTION).insert_one(
        {"name": "broken", "type": "sentinel-type-5512"}
    )

    assert [name for name, _ in second.list()] == ["good"]
    assert second.get("broken") is None


def test_an_update_or_delete_of_an_absent_provider_raises_key_error(docustore_pair):
    first, _ = _registries(docustore_pair)

    with pytest.raises(KeyError, match="absent"):
        first.update("absent", enabled=False)
    with pytest.raises(KeyError, match="absent"):
        first.delete("absent")
