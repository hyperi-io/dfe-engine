#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_oidc_loop_stays_free.py
#  Purpose:      An OIDC route waiting on the secret store leaves the event loop free
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Real app, real routes, and a secret store that holds each read at a gate.

The health probe does no I/O, so it answers the moment the event loop is free. Each case
sends one request that makes the engine read a provider's credential from the store, and
asks the probe to answer while that read is held. A route that read on the loop would
hold the probe with it.
"""

import threading
from collections.abc import Iterator
from typing import Any

import pytest

from dfe_engine.auth.oidc.models import OIDCProvider
from tests.support.slow_store import SlowSecretStore

_PROBE = "/livez"
# A backstop for each wait: only a failing run waits it out.
_WAIT_SECONDS = 10.0
# Outlasts every wait, so a held read is released by the test, never by the store.
_HOLD_SECONDS = _WAIT_SECONDS * 3
_PROVIDERS = "/api/v1/auth/oidc-providers"

_CREATE = {
    "name": "fresh",
    "type": "generic",
    "display_name": "Fresh",
    "issuer": "https://accounts.example.com",
    "client_id_env": "OIDC_CLIENT_ID",
}


def _provider(*, issuer: str = "", client_secret_path: str = "") -> OIDCProvider:
    """A generic provider in the registry, the engine's relying party not yet built around it."""
    return OIDCProvider(
        type="generic", client_id="client", issuer=issuer, client_secret_path=client_secret_path
    )


@pytest.fixture
def held_store(app, client, monkeypatch) -> Iterator[SlowSecretStore]:
    """The app's secret store, holding every read at a gate, over three providers in the registry.

    ``keep`` is enabled and reads its client secret from the store. Its issuer is a closed
    local port, so the discovery a login makes fails at once. ``no-issuer`` reads one too but
    names no issuer, which the login check reports without a network call. ``plain`` holds
    nothing in the store.
    """
    registry = app.state.oidc_provider_registry
    closed_port = "http://127.0.0.1:1"
    registry.create(
        "keep", _provider(issuer=closed_port, client_secret_path="oidc/keep/client_secret")
    )
    registry.create("no-issuer", _provider(client_secret_path="oidc/no-issuer/client_secret"))
    registry.create("plain", _provider(issuer=closed_port))
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    store = SlowSecretStore("test-secret", hold_seconds=_HOLD_SECONDS)
    app.state.dfe_secrets = store
    yield store
    store.gate.set()


def _answered_while_held(
    client: Any, store: SlowSecretStore, method: str, path: str, **request: Any
) -> int:
    """Send one request and probe the loop while its store read is held, then release it.

    Returns:
        The status the request answered once released.
    """
    handled: list[int] = []
    answered: list[tuple[int, bool]] = []

    def probe() -> None:
        status = client.get(_PROBE).status_code
        # True when no read has returned yet: the loop answered while the store held one.
        answered.append((status, store.gate_opened_by_test == []))

    worker = threading.Thread(
        target=lambda: handled.append(client.request(method, path, **request).status_code),
        daemon=True,
    )
    prober = threading.Thread(target=probe, daemon=True)
    worker.start()
    try:
        assert store.arrived.wait(timeout=_WAIT_SECONDS), f"{method} {path} never read the store"
        prober.start()
        prober.join(timeout=_WAIT_SECONDS)
        assert answered == [(200, True)], (
            f"{method} {path} held the event loop while the store was slow"
        )
        assert handled == []
    finally:
        store.gate.set()
        worker.join(timeout=_WAIT_SECONDS)
        if prober.ident is not None:
            prober.join(timeout=_WAIT_SECONDS)
    assert len(handled) == 1
    assert store.gate_opened_by_test
    assert all(store.gate_opened_by_test)
    return handled[0]


def test_a_login_for_a_provider_added_since_startup_leaves_the_loop_free(client, held_store):
    """The login route rebuilds the relying party when the registry holds a provider it lacks."""
    path = "/api/v1/auth/oidc/keep/login"

    status = _answered_while_held(client, held_store, "GET", path, params={"redirect": "false"})

    assert status != 404, "the relying party was never rebuilt around the provider"


def test_a_callback_for_a_provider_added_since_startup_leaves_the_loop_free(client, held_store):
    path = "/api/v1/auth/oidc/keep/callback"

    status = _answered_while_held(client, held_store, "GET", path)

    assert status == 401


def test_creating_a_provider_leaves_the_loop_free(client, held_store, admin_headers):
    status = _answered_while_held(
        client, held_store, "POST", _PROVIDERS, json=_CREATE, headers=admin_headers
    )

    assert status == 201


def test_updating_a_provider_leaves_the_loop_free(client, held_store, admin_headers):
    path = f"{_PROVIDERS}/keep"

    status = _answered_while_held(
        client, held_store, "PUT", path, json={"enabled": True}, headers=admin_headers
    )

    assert status == 200


def test_deleting_a_provider_leaves_the_loop_free(client, held_store, admin_headers):
    path = f"{_PROVIDERS}/plain"

    status = _answered_while_held(client, held_store, "DELETE", path, headers=admin_headers)

    assert status == 200


def test_checking_a_provider_login_config_leaves_the_loop_free(client, held_store, admin_headers):
    path = f"{_PROVIDERS}/no-issuer/verify-login"

    status = _answered_while_held(client, held_store, "GET", path, headers=admin_headers)

    assert status == 200
