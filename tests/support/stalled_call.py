#  Project:      dfe-engine
#  File:         tests/support/stalled_call.py
#  Purpose:      Stall one backend call and show the event loop still answers /livez
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A stalled backend call, and the proof that it holds up only the request waiting on it.

:class:`Stall` wraps one call a route makes to its backend. :func:`request_while_stalled`
sends the request, waits for the call to be reached, and asks ``/livez`` for an answer
while it is stalled. A route that made the call on the event loop holds ``/livez`` up
with it, as it would every request and the probes.

The client has to be the one that entered the app's lifespan: a client that was not
entered runs each request on a loop of its own, and a stalled call would then hold up
nothing.
"""

import contextlib
import threading
from collections.abc import Callable, Generator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response

PROBE_SECONDS = 5.0
"""How long ``/livez`` has to answer while a backend call is stalled."""

STALL_SECONDS = 30.0
"""How long a stalled call waits to be let go, should a failing test never let it go."""


class Stall:
    """Holds a call until :attr:`release` is set, after setting :attr:`reached`.

    The first :attr:`skip` calls pass straight through, so a later call of a route that
    makes several can be the one that stalls.
    """

    def __init__(self) -> None:
        self.reached = threading.Event()
        self.release = threading.Event()
        self.skip = 0
        self._lock = threading.Lock()

    def wait(self, *_args: object) -> None:
        with self._lock:
            if self.skip:
                self.skip -= 1
                return
        self.reached.set()
        self.release.wait(timeout=STALL_SECONDS)

    def call(self, real: Callable[..., Any]) -> Callable[..., Any]:
        def stalled(*args: object, **kwargs: object) -> Any:
            self.wait()
            return real(*args, **kwargs)

        return stalled

    def hold(self, real: Callable[..., Any]) -> Callable[..., Any]:
        @contextlib.contextmanager
        def stalled(*args: object, **kwargs: object) -> Generator[None]:
            self.wait()
            with real(*args, **kwargs):
                yield

        return stalled


type Setup = Callable[[Any, Stall, pytest.MonkeyPatch], None]


def registry_call(
    name: str, *, hold: bool = False, credentials: bool = False, skip: int = 0
) -> Setup:
    """Stall the org registry's call *name*, or its credential registry's.

    Args:
        name: The registry method to stall.
        hold: The method is a hold, a context manager, rather than a call.
        credentials: Stall the ingest credential registry's method, not the org registry's.
        skip: Calls to let through before the one that stalls.
    """

    def setup(app: Any, stall: Stall, monkeypatch: pytest.MonkeyPatch) -> None:
        registry = app.state.org_registry
        target = registry.ingest_credentials if credentials else registry
        real = getattr(target, name)
        stall.skip = skip
        monkeypatch.setattr(target, name, stall.hold(real) if hold else stall.call(real))

    return setup


def request_while_stalled(
    client: TestClient, stall: Stall, case: str, method: str, path: str, **request: Any
) -> Response:
    """Send a request that reaches the stalled call, and ask ``/livez`` to answer meanwhile.

    Args:
        client: The client that entered the app's lifespan.
        stall: The stall the request's route is to reach.
        case: What stalls, for the failure message.
        method: The request's HTTP method.
        path: The request's path.
        **request: The rest of the request, as ``client.request`` takes it.

    Returns:
        The stalled request's response, once the stall is let go.
    """
    with ThreadPoolExecutor(max_workers=2) as pool:
        waiting = pool.submit(client.request, method, path, **request)
        try:
            assert stall.reached.wait(timeout=PROBE_SECONDS), f"{case} never made the call"
            probe = pool.submit(client.get, "/livez")
            try:
                answered = probe.result(timeout=PROBE_SECONDS)
            except TimeoutError:
                pytest.fail(
                    f"/livez waited on the stalled call to {case}: it ran on the event loop"
                )
        finally:
            stall.release.set()

        assert answered.status_code == 200, answered.text
        return waiting.result(timeout=STALL_SECONDS)
