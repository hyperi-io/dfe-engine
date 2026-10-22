#  Project:      dfe-engine
#  File:         tests/integration/test_docustore_oidc_providers.py
#  Purpose:      OIDC providers written through one engine replica, served by another
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""OIDC providers across two engine replicas sharing one document store.

The replicas share the harness's document store (``mongo_uri``) and one secrets
directory (:mod:`tests.support.engine_replicas`). The IdP is a discovery document
served on loopback, so a login starts without leaving the host.
"""

import functools
import json
import threading
import time
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from tests.support.accounts import admin_on_its_own_password
from tests.support.engine_replicas import Deployment, engine_replicas
from tests.support.races import race, raised

pytestmark = pytest.mark.integration

_PROVIDERS = "/api/v1/auth/oidc-providers"
# A backstop for each wait: a replica that follows the store sees a write within its two-second floor.
_SEEN_WITHIN_SECONDS = 15.0
# Rounds of two replicas creating one name at once. Each round a loser that wrote its
# secret over the winner's has an even chance of being caught.
_CREATE_ROUNDS = 20


class _Discovery(BaseHTTPRequestHandler):
    """Answers the OIDC discovery request for an issuer at this server's own address."""

    def do_GET(self) -> None:
        if self.path != "/.well-known/openid-configuration":
            self.send_error(404)
            return
        issuer = f"http://{self.headers.get('Host', '')}"
        body = json.dumps(
            {
                "issuer": issuer,
                "authorization_endpoint": f"{issuer}/authorize",
                "token_endpoint": f"{issuer}/token",
                "jwks_uri": f"{issuer}/jwks",
            }
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        """Write no access line to stderr for each request."""


@pytest.fixture
def issuer(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """The URL of an IdP serving its discovery document on loopback."""
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Discovery)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


@pytest.fixture
def deployment(mongo_uri: str, tmp_path: Path) -> Iterator[Deployment]:
    """Replicas started through it stop, and its database is dropped, at teardown."""
    with engine_replicas(root=tmp_path, uri=mongo_uri) as deployed:
        yield deployed


def _provider(name: str, issuer: str, **fields: Any) -> dict[str, Any]:
    return {"name": name, "type": "generic", "issuer": issuer, "client_id": "dfe", **fields}


def _login(replica: SimpleNamespace, provider: str) -> httpx.Response:
    return replica.client.get(f"/api/v1/auth/oidc/{provider}/login", params={"redirect": "false"})


def _until(check: Callable[[], bool], *, failure: str) -> None:
    """Poll *check* until it holds, failing the test once the backstop passes."""
    deadline = time.monotonic() + _SEEN_WITHIN_SECONDS
    while not check():
        if time.monotonic() > deadline:
            pytest.fail(f"{failure} within {_SEEN_WITHIN_SECONDS:.0f}s")
        time.sleep(0.2)


def _client_secret(replica: SimpleNamespace, provider: str) -> str | None:
    """The client secret *replica*'s relying party registered for *provider*, once a login asked for it."""
    if _login(replica, provider).status_code != 200:
        return None
    return replica.app.state.oidc_rp._oauth.create_client(provider).client_secret


def _seed(deployment: Deployment, name: str, issuer: str, **fields: Any) -> None:
    body = {"type": "generic", "issuer": issuer, "client_id": "dfe", **fields}
    (deployment.providers_seed / f"{name}.yaml").write_text(json.dumps(body), encoding="utf-8")


def _listed(replica: SimpleNamespace) -> list[str]:
    resp = replica.client.get(_PROVIDERS, headers=replica.headers)
    assert resp.status_code == 200, resp.text
    return [item["name"] for item in resp.json()["items"]]


class TestProviderWritesAcrossReplicas:
    """A provider written through one replica changes what logins the other serves."""

    def test_a_provider_added_through_one_replica_serves_logins_through_the_other(
        self, deployment, issuer
    ):
        first, second = deployment.replica("a"), deployment.replica("b")
        assert _login(second, "acme").status_code == 404

        created = first.client.post(
            _PROVIDERS, json=_provider("acme", issuer), headers=first.headers
        )

        assert created.status_code == 201, created.text
        _until(
            lambda: _login(second, "acme").status_code == 200,
            failure="the second replica never served the provider the first added",
        )
        url = _login(second, "acme").json()["authorization_url"]
        assert url.startswith(f"{issuer}/authorize?")

    def test_a_provider_disabled_through_one_replica_refuses_logins_through_the_other(
        self, deployment, issuer
    ):
        first, second = deployment.replica("a"), deployment.replica("b")
        created = first.client.post(
            _PROVIDERS, json=_provider("acme", issuer), headers=first.headers
        )
        assert created.status_code == 201, created.text
        _until(
            lambda: _login(second, "acme").status_code == 200,
            failure="the second replica never served the provider the first added",
        )

        disabled = first.client.put(
            f"{_PROVIDERS}/acme", json={"enabled": False}, headers=first.headers
        )

        assert disabled.status_code == 200, disabled.text
        _until(
            lambda: _login(second, "acme").status_code == 404,
            failure="the second replica kept serving a provider the first disabled",
        )

    def test_a_secret_rotated_through_one_replica_reaches_the_others_relying_party(
        self, deployment, issuer
    ):
        first, second = deployment.replica("a"), deployment.replica("b")
        created = first.client.post(
            _PROVIDERS, json=_provider("acme", issuer, client_secret="first"), headers=first.headers
        )
        assert created.status_code == 201, created.text
        _until(
            lambda: _client_secret(second, "acme") == "first",
            failure="the second replica never registered the provider's secret",
        )

        rotated = first.client.put(
            f"{_PROVIDERS}/acme", json={"client_secret": "second"}, headers=first.headers
        )

        assert rotated.status_code == 200, rotated.text
        _until(
            lambda: _client_secret(second, "acme") == "second",
            failure="the second replica kept the secret the first rotated out",
        )


class TestTwoReplicasCreatingOneName:
    """Of two creates of one name at once, one wins, and its provider holds its own secret."""

    def test_the_winner_keeps_its_secret(self, deployment, issuer):
        replicas = [deployment.replica("a"), deployment.replica("b")]
        winners: dict[str, str] = {}
        for round_ in range(_CREATE_ROUNDS):
            name = f"acme-{round_}"
            calls = [
                functools.partial(
                    replica.client.post,
                    _PROVIDERS,
                    json=_provider(name, issuer, client_secret=f"from-{index}"),
                    headers=replica.headers,
                )
                for index, replica in enumerate(replicas)
            ]

            results = race(calls)

            responses = [result for result in results if not isinstance(result, BaseException)]
            assert raised(results) == []
            statuses = [response.status_code for response in responses]
            assert sorted(statuses) == [201, 409], [response.text for response in responses]
            winners[name] = f"from-{statuses.index(201)}"

        for replica in replicas:
            for name, secret in winners.items():
                _until(
                    lambda replica=replica, name=name, secret=secret: (
                        _client_secret(replica, name) == secret
                    ),
                    failure=f"{name} never resolved the secret its winning create sent",
                )


class TestProviderSeed:
    """Every boot writes the seeded providers over the stored ones, config winning."""

    @pytest.mark.parametrize("round_", range(3))
    def test_two_replicas_booting_at_once_leave_one_of_each_provider(
        self, deployment, issuer, round_
    ):
        _seed(deployment, "acme", issuer)
        _seed(deployment, "okta", issuer, display_name="Okta")
        apps = [deployment.app("a"), deployment.app("b")]

        results = race([functools.partial(deployment.start, app) for app in apps])

        assert raised(results) == []
        for app in apps:
            admin_on_its_own_password(app)
        replicas = [deployment.replica(name) for name in ("c", "d")]
        assert [_listed(replica) for replica in replicas] == [["acme", "okta"]] * 2

    def test_the_next_boot_puts_back_a_seeded_provider_changed_through_the_api(
        self, deployment, issuer
    ):
        _seed(deployment, "acme", issuer, display_name="Acme")
        first = deployment.replica("a")
        edited = first.client.put(
            f"{_PROVIDERS}/acme",
            json={"display_name": "Edited", "enabled": False},
            headers=first.headers,
        )
        assert edited.status_code == 200, edited.text

        deployment.replica("b")

        stored = first.client.get(f"{_PROVIDERS}/acme", headers=first.headers).json()
        assert (stored["display_name"], stored["enabled"]) == ("Acme", True)

    def test_a_provider_the_seed_does_not_name_is_left_alone(self, deployment, issuer):
        _seed(deployment, "acme", issuer)
        first = deployment.replica("a")
        created = first.client.post(
            _PROVIDERS, json=_provider("local", issuer), headers=first.headers
        )
        assert created.status_code == 201, created.text

        second = deployment.replica("b")

        assert _listed(second) == ["acme", "local"]
