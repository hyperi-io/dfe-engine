#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_orgs_off_loop.py
#  Purpose:      A stalled store holds up only the org request waiting on it, never the event loop
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A stalled backend call holds up only the request waiting on it.

Each case stalls one call an org or ingest credential route makes to its backend, then
asks ``/livez`` for an answer while it is stalled. A route that made the call on the
event loop holds ``/livez`` up with it, as it would every request and the probes.
"""

import contextlib
import threading
from collections.abc import Callable, Generator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from dfe_engine.api.deps import get_clickhouse_client

PROBE_SECONDS = 5.0
"""How long ``/livez`` has to answer while a backend call is stalled."""

STALL_SECONDS = 30.0
"""How long a stalled call waits to be let go, should a failing test never let it go."""

ORGS = "/api/v1/orgs"
CREDENTIALS = f"{ORGS}/acme/ingest-credentials"


class _Stall:
    """Holds a call until :attr:`release` is set, after setting :attr:`reached`."""

    def __init__(self) -> None:
        self.reached = threading.Event()
        self.release = threading.Event()

    def wait(self, *_args: object) -> None:
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


type Setup = Callable[[Any, _Stall, pytest.MonkeyPatch], None]


def _registry_call(name: str, *, hold: bool = False, credentials: bool = False) -> Setup:
    """Stall the org registry's call *name*, or its credential registry's."""

    def setup(app: Any, stall: _Stall, monkeypatch: pytest.MonkeyPatch) -> None:
        registry = app.state.org_registry
        target = registry.ingest_credentials if credentials else registry
        real = getattr(target, name)
        monkeypatch.setattr(target, name, stall.hold(real) if hold else stall.call(real))

    return setup


def _change_listener(app: Any, stall: _Stall, _monkeypatch: pytest.MonkeyPatch) -> None:
    app.state.org_registry.on_change(stall.wait)


def _clickhouse(app: Any, stall: _Stall, _monkeypatch: pytest.MonkeyPatch) -> None:
    class Stalled:
        def execute(self, sql: str, parameters: object = None, settings: object = None) -> list:
            stall.wait()
            return []

    app.dependency_overrides[get_clickhouse_client] = Stalled


# What stalls, and the request that waits on it.
_CASES: dict[str, tuple[Setup, str, str, dict | None]] = {
    "list orgs": (_registry_call("list"), "GET", ORGS, None),
    "get an org": (_registry_call("get"), "GET", f"{ORGS}/acme", None),
    "hold a create": (_registry_call("reserved", hold=True), "POST", ORGS, {"name": "beta"}),
    "create an org": (_registry_call("create"), "POST", ORGS, {"name": "beta"}),
    "a change listener": (_change_listener, "POST", ORGS, {"name": "beta"}),
    "hold a delete": (_registry_call("deleting", hold=True), "DELETE", f"{ORGS}/acme", None),
    "delete an org": (_registry_call("delete"), "DELETE", f"{ORGS}/acme", None),
    "read tenant ids": (_clickhouse, "GET", f"{ORGS}/available-ids", None),
    "apply a credential": (_registry_call("get"), "PUT", f"{CREDENTIALS}/agents", {}),
    "delete a credential": (
        _registry_call("get", credentials=True),
        "DELETE",
        f"{CREDENTIALS}/agents",
        None,
    ),
    "rotate a credential": (
        _registry_call("get", credentials=True),
        "POST",
        f"{CREDENTIALS}/agents/rotate",
        None,
    ),
}


@pytest.fixture
def acme(client, admin_headers) -> None:
    """Org acme, owning ingest credential agents."""
    made = client.post(ORGS, json={"name": "acme"}, headers=admin_headers)
    assert made.status_code == 201, made.text
    minted = client.put(
        f"{CREDENTIALS}/agents", json={}, headers={**admin_headers, "If-None-Match": "*"}
    )
    assert minted.status_code == 201, minted.text


@pytest.mark.parametrize("case", list(_CASES))
def test_a_stalled_call_holds_up_only_its_own_request(
    case, client, app, admin_headers, acme, monkeypatch
):
    setup, method, path, body = _CASES[case]
    stall = _Stall()
    setup(app, stall, monkeypatch)

    with ThreadPoolExecutor(max_workers=2) as pool:
        waiting = pool.submit(client.request, method, path, json=body, headers=admin_headers)
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
        done = waiting.result(timeout=STALL_SECONDS)
        assert done.status_code < 500, done.text
