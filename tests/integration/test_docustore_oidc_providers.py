#  Project:      dfe-engine
#  File:         tests/integration/test_docustore_oidc_providers.py
#  Purpose:      OIDC providers written through one engine replica, served by another
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""OIDC providers across two engine replicas sharing one document store.

Each replica is a whole engine app with its own config directory. They share the
harness's document store (``mongo_uri``) and one secrets directory, as replicas share
a document store and a secrets backend. The IdP is a discovery document served on
loopback, so a login starts without leaving the host.
"""

import functools
import json
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dfe_engine.api.app import create_app
from dfe_engine.api.deps import _registries, create_access_token
from dfe_engine.settings import (
    AccountStoreSettings,
    APISettings,
    AuthSettings,
    ClickHouseSettings,
    DFESettings,
    HuntsSettings,
    LocalAuthSettings,
    SchemasSettings,
    SecretsSettings,
    ServicesSettings,
    SigmaSettings,
    SourceSettings,
)
from dfe_engine.store.documents import DocuStore
from tests.support.accounts import admin_on_its_own_password
from tests.support.core_sources import write_landing_definition
from tests.support.races import race, raised

pytestmark = pytest.mark.integration

_PROVIDERS = "/api/v1/auth/oidc-providers"
_ADMIN_PASSWORD = "issued-admin-password-1"
# A backstop for each wait: a replica that follows the store sees a write within its two-second floor.
_SEEN_WITHIN_SECONDS = 15.0


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


class _Deployment:
    """Engine replicas over one document-store database and one secrets directory.

    Attributes:
        seed: The providers directory every replica's ``auth.seed_dir`` holds.
    """

    def __init__(self, *, root: Path, uri: str, stack: ExitStack) -> None:
        self._root = root
        self._uri = uri
        self._stack = stack
        self.database = f"dfe_engine_test_{uuid.uuid4().hex[:8]}"
        self._secrets = root / "secrets"
        self._secrets.mkdir()
        self.seed = root / "seed" / "oidc-providers"
        self.seed.mkdir(parents=True)

    def app(self, name: str) -> FastAPI:
        """An engine app for replica *name*, not yet started."""
        return create_app(settings=self._settings(name))

    def start(self, app: FastAPI) -> TestClient:
        """Run *app*'s startup, which seeds the providers, and return its client."""
        client = TestClient(app, raise_server_exceptions=False)
        client.__enter__()
        self._stack.push(client.__exit__)
        return client

    def replica(self, name: str) -> SimpleNamespace:
        """A started replica, its admin signed in."""
        app = self.app(name)
        client = self.start(app)
        admin_on_its_own_password(app)
        token = create_access_token(
            data={"sub": "admin", "org_id": "test-org", "roles": ["admin"]},
            settings=app.state.settings,
        )
        return SimpleNamespace(app=app, client=client, headers={"Authorization": f"Bearer {token}"})

    def _settings(self, name: str) -> DFESettings:
        root = self._root / name
        dirs = {
            key: root / key for key in ("sources", "services", "rules", "hunts", "auth", "schemas")
        }
        for path in dirs.values():
            path.mkdir(parents=True)
        write_landing_definition(schemas_dir=dirs["schemas"])
        return DFESettings(
            config_dir=str(root),
            clickhouse=ClickHouseSettings(bootstrap_tables=False),
            source=SourceSettings(sources_dir=str(dirs["sources"])),
            services=ServicesSettings(config_yaml_dir=str(dirs["services"])),
            schemas=SchemasSettings(schemas_dir=str(dirs["schemas"])),
            hunts=HuntsSettings(rules_dir=str(dirs["rules"]), hunt_dir=str(dirs["hunts"])),
            sigma=SigmaSettings(enabled=False),
            auth=AuthSettings(
                enabled=True,
                auth_dir=str(dirs["auth"]),
                store_backend="document",
                accounts_store=AccountStoreSettings(uri=self._uri, database=self.database),
                seed_dir=str(self.seed.parent),
                local=LocalAuthSettings(admin_password=_ADMIN_PASSWORD),
            ),
            secrets=SecretsSettings(provider="file", path=str(self._secrets)),
            api=APISettings(
                jwt_secret="test-secret-key-for-integration-hmac32", jwt_expire_minutes=30
            ),
        )


@pytest.fixture
def deployment(mongo_uri: str, tmp_path: Path) -> Iterator[_Deployment]:
    """Replicas started through it stop, and its database is dropped, at teardown."""
    with ExitStack() as stack:
        deployed = _Deployment(root=tmp_path, uri=mongo_uri, stack=stack)
        try:
            yield deployed
        finally:
            stack.close()
            store = DocuStore(mongo_uri, deployed.database)
            store.drop_database(deployed.database)
            store.close()
            _registries.clear()


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


def _seed(deployment: _Deployment, name: str, issuer: str, **fields: Any) -> None:
    body = {"type": "generic", "issuer": issuer, "client_id": "dfe", **fields}
    (deployment.seed / f"{name}.yaml").write_text(json.dumps(body), encoding="utf-8")


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
