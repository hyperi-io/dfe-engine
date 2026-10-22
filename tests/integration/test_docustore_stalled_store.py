#  Project:      dfe-engine
#  File:         tests/integration/test_docustore_stalled_store.py
#  Purpose:      A document store that stops answering holds only the requests that read it
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""An engine whose document store stops answering keeps serving every other request.

The replica reaches the harness's document store (``mongo_uri``) through a relay the
test holds (:mod:`tests.support.pausable_relay`), so a request that reads the store
waits on a real connection that has gone quiet. A store call made on the event loop
holds the loop with it, and then nothing else is answered until the store is.
"""

import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest

from tests.support.engine_replicas import Deployment, engine_replicas
from tests.support.event_loop import HELD_SECONDS
from tests.support.pausable_relay import PausableRelay

pytestmark = pytest.mark.integration

# Time for a request sent once the store is held to reach its first store read.
_SETTLE_SECONDS = 0.5


class _Timed:
    """A request sent from its own thread, and when its answer came."""

    def __init__(self, send: Callable[[], httpx.Response]) -> None:
        self.response: httpx.Response | None = None
        self.answered_at = 0.0
        self.done = threading.Event()
        self._send = send
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            self.response = self._send()
        finally:
            self.answered_at = time.monotonic()
            self.done.set()

    def join(self) -> None:
        self._thread.join(timeout=HELD_SECONDS * 4)


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


@pytest.fixture
def deployment(mongo_uri: str, relay: PausableRelay, tmp_path: Path) -> Iterator[Deployment]:
    """Replicas that reach the store through *relay*, released again before teardown."""
    with engine_replicas(root=tmp_path, uri=relay.relayed_uri(mongo_uri)) as deployed:
        try:
            yield deployed
        finally:
            relay.release()


def test_a_request_waiting_on_the_store_leaves_liveness_answering(deployment, relay):
    replica = deployment.replica("a")
    relay.hold()
    try:
        reader = _Timed(lambda: replica.client.get("/api/v1/auth/me", headers=replica.headers))
        assert relay.arrived.wait(HELD_SECONDS), "the request never reached the store"
        time.sleep(_SETTLE_SECONDS)
        liveness = _Timed(lambda: replica.client.get("/livez"))
        liveness.done.wait(HELD_SECONDS)
    finally:
        released_at = time.monotonic()
        relay.release()
    reader.join()
    liveness.join()

    assert liveness.response is not None
    assert liveness.response.status_code == 200
    assert liveness.answered_at < released_at, "liveness waited for the stalled store"
    assert reader.response is not None
    assert reader.response.status_code == 200, reader.response.text
    assert reader.answered_at > released_at, "the request never waited on the store"
