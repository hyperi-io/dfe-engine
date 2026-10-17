#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_oidc/test_directory_secret_reads.py
#  Purpose:      A directory adapter reads its credential from the secret store off the event loop
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""A directory adapter reads its credential from the secret store off the event loop.

The store is a network read (OpenBao), so a slow one held on the loop stalls every
API request. Each case holds the store's read at a gate and checks that the loop keeps
running meanwhile and that the read ran on a worker thread.
"""

import asyncio
import threading
from collections.abc import Callable, Coroutine, Iterator
from itertools import product
from typing import Any

import pytest

from dfe_engine.auth.oidc.adapters.base import OIDCGroupAdapter
from dfe_engine.auth.oidc.adapters.entra import EntraAdapter
from dfe_engine.auth.oidc.adapters.google import GoogleAdapter
from dfe_engine.auth.oidc.adapters.okta import OktaAdapter
from dfe_engine.auth.oidc.models import GroupResolutionConfig, OIDCProvider
from dfe_engine.secrets import DfeSecrets
from tests.support.event_loop import ticks_while_held
from tests.support.slow_store import SlowSecretStore
from tests.unit.test_auth.test_oidc.local_directory_api import LocalDirectoryApi, directory_service
from tests.unit.test_auth.test_oidc.local_graph import TENANT, LocalGraph
from tests.unit.test_auth.test_oidc.local_okta import LocalOkta

USER = "00000000-0000-4000-8000-0000000000aa"
OKTA_GROUP = {"id": "00g1soc", "profile": {"name": "SOC Analysts", "description": "SOC"}}
GRAPH_GROUP = {"id": "11111111-0000-4000-8000-000000000001", "displayName": "dfe-admins"}


class _OktaOverPlainHttp(OktaAdapter):
    """Okta that resolves its API token through the store and sends to a plain-http stand-in."""

    def __init__(self, provider: OIDCProvider, *, secrets: DfeSecrets, base_url: str) -> None:
        super().__init__(provider, secrets=secrets)
        self._base_url = base_url

    def _api_base_and_headers(self) -> tuple[str | None, dict[str, str] | None]:
        _, headers = super()._api_base_and_headers()
        return self._base_url, headers


class _GoogleOverLocalDirectory(GoogleAdapter):
    """Google that resolves its service account through the store and reads a local Directory API."""

    def __init__(self, provider: OIDCProvider, *, secrets: DfeSecrets, base_url: str) -> None:
        super().__init__(provider, secrets=secrets)
        self._base_url = base_url

    def _get_service(self) -> Any | None:
        if not self._service_account_json.value:
            return None
        return directory_service(self._base_url)


class _EntraWithAppToken(EntraAdapter):
    """Entra that resolves its Graph credentials through the store and is issued a token for them."""

    def _get_token(self) -> str | None:
        return "app-token" if self._graph_credentials.configured else None


@pytest.fixture
def serve() -> Iterator[Callable[[Any], Any]]:
    """Start a local directory service for the test and stop it afterwards."""
    started: list[Any] = []

    def start(server: Any) -> Any:
        server.start()
        started.append(server)
        return server

    yield start
    for server in started:
        server.stop()


def _okta(serve: Callable[[Any], Any], store: DfeSecrets) -> OIDCGroupAdapter:
    okta = serve(LocalOkta(pages=[[OKTA_GROUP]]))
    provider = OIDCProvider(
        type="okta",
        issuer="https://example.okta.com",
        groups=GroupResolutionConfig(
            mode="api",
            okta_domain="example.okta.com",
            api_token_path="oidc/acme/groups_api_token",
        ),
    )
    return _OktaOverPlainHttp(provider, secrets=store, base_url=okta.base_url)


def _entra(serve: Callable[[Any], Any], store: DfeSecrets) -> OIDCGroupAdapter:
    graph = serve(LocalGraph())
    graph.pages[TENANT] = [[GRAPH_GROUP]]
    provider = OIDCProvider(
        type="entra_id",
        issuer="https://login.microsoftonline.com/test-tenant/v2.0",
        client_id="client",
        groups=GroupResolutionConfig(
            mode="api",
            tenant_id="tenant",
            client_secret_path="oidc/acme/groups_client_secret",
        ),
    )
    adapter = _EntraWithAppToken(provider, secrets=store)
    adapter.GRAPH_BASE = graph.base_url
    return adapter


def _google(serve: Callable[[Any], Any], store: DfeSecrets) -> OIDCGroupAdapter:
    directory = serve(LocalDirectoryApi(pages=1))
    provider = OIDCProvider(
        type="google",
        issuer="https://accounts.google.com",
        groups=GroupResolutionConfig(
            mode="api", service_account_json_path="oidc/acme/groups_service_account_json"
        ),
    )
    return _GoogleOverLocalDirectory(provider, secrets=store, base_url=directory.base_url)


_ADAPTERS = {"okta": _okta, "entra": _entra, "google": _google}

_OPERATIONS: dict[str, Callable[[OIDCGroupAdapter], Coroutine[Any, Any, object]]] = {
    "list_all_groups": lambda adapter: adapter.list_all_groups(),
    "resolve_user_groups": lambda adapter: adapter.resolve_user_groups(USER),
    "test_connection": lambda adapter: adapter.test_connection(),
}

_CASES = list(product(_ADAPTERS, _OPERATIONS))


@pytest.mark.parametrize(("provider", "operation"), _CASES, ids=[f"{p}-{o}" for p, o in _CASES])
async def test_a_slow_credential_read_does_not_block_the_event_loop(
    serve: Callable[[Any], Any], provider: str, operation: str
):
    store = SlowSecretStore("test-secret")
    adapter = _ADAPTERS[provider](serve, store)

    running = asyncio.create_task(_OPERATIONS[operation](adapter))
    ticks = await ticks_while_held(arrived=store.arrived, gate=store.gate)
    await running

    # The event loop ran while the read was held: the test opened the gate, the wait did not time out.
    assert store.gate_opened_by_test == [True]
    assert ticks >= 5
    assert threading.get_ident() not in store.read_threads
