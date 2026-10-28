#  Project:      dfe-engine
#  File:         tests/integration/test_docustore_socket_timeout.py
#  Purpose:      A document store that stops answering mid-call fails the call within the timeout
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A real document store that answers the handshake and then stops answering a call.

The store is the harness's (``mongo_uri``), reached through a relay the test holds
(:mod:`tests.support.pausable_relay`). The connection is open and logged in, so the
call reaches a store that never replies, as a stalled store looks to the engine.
"""

import threading
import time
from collections.abc import Iterator
from urllib.parse import urlsplit

import pytest
from pymongo.errors import NetworkTimeout

from dfe_engine.store.documents import DocuStore
from tests.support.pausable_relay import PausableRelay

pytestmark = pytest.mark.integration

_SOCKET_TIMEOUT_SECONDS = 1.0
# Far above the one-second timeout plus the client's own bookkeeping.
_CEILING_SECONDS = 10.0


@pytest.fixture
def relay(mongo_uri: str) -> Iterator[PausableRelay]:
    parts = urlsplit(mongo_uri)
    assert parts.hostname is not None, "a single-host store URI"
    assert parts.port is not None, "a store URI that names its port"
    relayed = PausableRelay((parts.hostname, parts.port))
    try:
        yield relayed
    finally:
        relayed.close()


def test_a_call_the_store_stops_answering_fails_within_the_timeout(mongo_uri, relay):
    store = DocuStore(
        relay.relayed_uri(mongo_uri),
        "dfe_engine_test_socket_timeout",
        socket_timeout=_SOCKET_TIMEOUT_SECONDS,
    )
    outcome: dict[str, float] = {}

    def call() -> None:
        started = time.monotonic()
        try:
            store.ping()
        except NetworkTimeout:
            outcome["failed_after"] = time.monotonic() - started
        else:
            outcome["answered_after"] = time.monotonic() - started

    try:
        store.ping()
        relay.hold()
        caller = threading.Thread(target=call, daemon=True)
        caller.start()
        caller.join(_CEILING_SECONDS)
        relay.release()
        caller.join(_CEILING_SECONDS)
    finally:
        relay.release()
        store.close()

    assert "failed_after" in outcome, f"the call waited for the store to answer: {outcome}"
    assert outcome["failed_after"] < _CEILING_SECONDS
