#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_oidc_directory_outage_login.py
#  Purpose:      A directory outage at OIDC login keeps the account's stored groups, a refusal does not
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A login whose token carries no groups, and whose directory lookup fails.

An Entra overage token carries a pointer at Graph instead of its groups, a Google
token carries none, and an Okta provider can be set to read them at login too.
When the directory has an outage (a 5xx, or no answer) the callback runs on the
account's stored groups: it neither fails nor rewrites them, and its sessions
stay. When the directory refuses (a 401 or 403, a declined scope) the login is
judged on no directory groups, so a user cannot keep access by declining the
scope the lookup needs. The RP's token handling and the adapters are the real
ones, against the local Graph, Okta and Cloud Identity services.
"""

from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from dfe_engine.auth.oidc.adapters.base import OIDCGroupAdapter
from dfe_engine.auth.oidc.adapters.google import GoogleAdapter
from dfe_engine.auth.oidc.models import GroupResolutionConfig, OIDCProvider
from dfe_engine.auth.oidc.registry import OIDCProviderRegistry
from dfe_engine.auth.oidc.rp import (
    DEFAULT_DIRECTORY_DEADLINE_SECONDS,
    NormalizedIdentity,
    OidcRelyingParty,
)
from tests.support.loopback import CountingListener
from tests.support.oidc import serve_provider
from tests.unit.test_auth.test_oidc.local_cloud_identity import LocalCloudIdentity
from tests.unit.test_auth.test_oidc.local_directory import AppTokenEntra, fast_http
from tests.unit.test_auth.test_oidc.local_graph import LocalGraph
from tests.unit.test_auth.test_oidc.local_okta import LocalOkta, okta_adapter

PROVIDER = "acme-idp"
OID = "00000000-0000-0000-0000-0000000a11ce"
SUBJECT = "pairwise-sub-alice"
ADMINS_ID = "0d1e2f30-0000-4000-8000-00000000ad31"
FAILING = {"error": {"code": "ServiceUnavailable", "status": "UNAVAILABLE"}}
SCOPE_REFUSED = {"error": {"code": 403, "status": "PERMISSION_DENIED"}}

USERINFO = {"email": "alice@acme.test", "name": "Alice", "oid": OID, "sub": SUBJECT}

PROVIDERS: dict[str, OIDCProvider] = {
    "entra": OIDCProvider(type="entra_id", issuer="https://login.microsoftonline.com/t/v2.0"),
    "google": OIDCProvider(
        type="google",
        issuer="https://accounts.google.com",
        groups=GroupResolutionConfig(enrich_on_login=True, mode="api"),
    ),
    "okta": OIDCProvider(
        type="okta",
        issuer="https://acme.okta.com",
        groups=GroupResolutionConfig(enrich_on_login=True, mode="api", okta_domain="acme.okta.com"),
    ),
}

# The claims Authlib hands back once it has validated each provider's id_token: none carry groups.
TOKENS: dict[str, dict[str, Any]] = {
    "entra": {"userinfo": {**USERINFO, "_claim_names": {"groups": "src1"}}},
    "google": {"access_token": "user-token", "userinfo": USERINFO},
    "okta": {"userinfo": USERINFO},
}


def _serving(kind: str, status: int) -> tuple[str, Callable[[], None]]:
    """A local directory of *kind* answering every lookup with *status*."""
    if kind == "entra":
        graph = LocalGraph()
        graph.refuse[f"users/{OID}"] = (status, FAILING)
        graph.start()
        return graph.base_url, graph.stop
    if kind == "okta":
        okta = LocalOkta(pages=[[]])
        okta.fail_page = 0
        okta.fail_status = status
        okta.start()
        return okta.base_url, okta.stop
    directory = LocalCloudIdentity()
    for method in ("searchTransitiveGroups", "searchDirectGroups"):
        directory.refuse[method] = (status, SCOPE_REFUSED if status == 403 else FAILING)
    directory.start()
    return directory.base_url, directory.stop


def _adapter(kind: str, base_url: str) -> OIDCGroupAdapter:
    """The real adapter for *kind*, reading its directory at *base_url*."""
    if kind == "entra":
        adapter = AppTokenEntra(PROVIDERS["entra"])
        adapter.GRAPH_BASE = base_url
        return adapter
    if kind == "okta":
        return okta_adapter(base_url)
    google = GoogleAdapter(PROVIDERS["google"], access_token="user-token")
    google.CLOUD_IDENTITY_BASE = base_url
    return google


class _DirectRp(OidcRelyingParty):
    """The real RP token handling, fed a validated token instead of an IdP exchange."""

    def __init__(self, registry: OIDCProviderRegistry, kind: str, deadline: float) -> None:
        super().__init__(registry, directory_deadline_seconds=deadline)
        self._kind = kind

    def has_provider(self, provider_name: str) -> bool:
        return provider_name == PROVIDER

    async def handle_callback(self, provider_name: str, request) -> NormalizedIdentity:
        return await self._identity_from_token(
            client=None,
            provider=PROVIDERS[self._kind],
            provider_name=provider_name,
            token=TOKENS[self._kind],
        )


@pytest.fixture
def directory(app, client: TestClient, tmp_path, monkeypatch) -> Iterator[Callable[..., None]]:
    """A setter pointing the login at a directory of one kind that fails one way."""
    app.state.group_store.create(
        "idp-admins", roles=["admin"], source_provider=PROVIDER, source_id=ADMINS_ID
    )
    stops: list[Callable[[], None]] = []

    def _point(
        kind: str,
        *,
        status: int | None = None,
        base_url: str = "",
        deadline: float = DEFAULT_DIRECTORY_DEADLINE_SECONDS,
    ) -> None:
        if status is not None:
            base_url, stop = _serving(kind, status)
            stops.append(stop)
        adapter = _adapter(kind, base_url)
        monkeypatch.setattr("dfe_engine.auth.oidc.adapters.get_adapter", lambda *_a, **_k: adapter)
        registry = OIDCProviderRegistry(tmp_path / "rp-registry")
        if app.state.oidc_provider_registry.get(PROVIDER) is None:
            serve_provider(app, PROVIDER)
        app.state.oidc_rp = _DirectRp(registry, kind, deadline)

    yield _point
    for stop in stops:
        stop()


@pytest.fixture
def silent(monkeypatch) -> Iterator[str]:
    """A directory origin that accepts every connection and never answers, and a client that gives up on it."""
    fast_http(monkeypatch)
    listener = CountingListener(hold=True)
    yield f"http://127.0.0.1:{listener.port}"
    listener.close()


def _seed_account(app) -> Any:
    """Alice's account as her last successful login left it, holding the admins group."""
    accounts = app.state.account_store
    accounts.create(SUBJECT, "", groups=[ADMINS_ID])
    return accounts.update(SUBJECT, external=True, source_provider=PROVIDER, subject=SUBJECT)


KINDS = sorted(PROVIDERS)


class TestAnOutageAtLogin:
    @pytest.mark.parametrize("kind", KINDS)
    @pytest.mark.parametrize("status", [503, 500])
    def test_a_failing_directory_logs_in_on_the_stored_groups(
        self, app, client: TestClient, directory, kind, status
    ):
        before = _seed_account(app)
        directory(kind, status=status)

        resp = client.get(f"/api/v1/auth/oidc/{PROVIDER}/callback")

        assert resp.status_code == 200, resp.text
        assert resp.json()["groups"] == [ADMINS_ID]
        stored = app.state.account_store.get(SUBJECT)
        assert stored.groups == [ADMINS_ID]
        assert stored.session_marker() == before.session_marker()

    @pytest.mark.parametrize("kind", KINDS)
    def test_a_directory_that_never_answers_logs_in_on_the_stored_groups(
        self, app, client: TestClient, directory, silent, kind
    ):
        before = _seed_account(app)
        directory(kind, base_url=silent)

        resp = client.get(f"/api/v1/auth/oidc/{PROVIDER}/callback")

        assert resp.status_code == 200, resp.text
        stored = app.state.account_store.get(SUBJECT)
        assert (stored.groups, stored.session_marker()) == ([ADMINS_ID], before.session_marker())

    @pytest.mark.parametrize("kind", ["entra", "okta"])
    def test_a_directory_still_reading_at_the_deadline_logs_in_on_the_stored_groups(
        self, app, client: TestClient, directory, kind
    ):
        """The HTTP client keeps its full timeout here, so only the deadline ends the lookup."""
        before = _seed_account(app)
        listener = CountingListener(hold=True)
        try:
            directory(kind, base_url=f"http://127.0.0.1:{listener.port}", deadline=0.05)
            resp = client.get(f"/api/v1/auth/oidc/{PROVIDER}/callback")
        finally:
            listener.close()

        assert resp.status_code == 200, resp.text
        assert resp.json()["groups"] == [ADMINS_ID]
        stored = app.state.account_store.get(SUBJECT)
        assert (stored.groups, stored.session_marker()) == ([ADMINS_ID], before.session_marker())

    def test_a_first_login_with_nothing_stored_still_gets_no_role(
        self, app, client: TestClient, directory
    ):
        directory("entra", status=503)

        resp = client.get(f"/api/v1/auth/oidc/{PROVIDER}/callback")

        assert resp.status_code == 401, resp.text
        assert resp.json()["code"] == "no_role_granted"
        assert app.state.account_store.get(SUBJECT) is None


class TestARefusalAtLogin:
    @pytest.mark.parametrize("kind", KINDS)
    @pytest.mark.parametrize("status", [401, 403])
    def test_a_refusing_directory_leaves_no_groups_to_log_in_on(
        self, app, client: TestClient, directory, kind, status
    ):
        """A Google user declining the groups scope is the 403 case: no access is kept."""
        _seed_account(app)
        directory(kind, status=status)

        resp = client.get(f"/api/v1/auth/oidc/{PROVIDER}/callback")

        assert resp.status_code == 401, resp.text
        assert resp.json()["code"] == "no_role_granted"
        assert app.state.account_store.get(SUBJECT).groups == []

    def test_a_refusal_that_answers_slowly_is_still_a_refusal(
        self, app, client: TestClient, directory
    ):
        """A directory that answers late has answered: only no answer at all keeps the groups."""
        _seed_account(app)
        okta = LocalOkta(pages=[[]])
        okta.fail_page = 0
        okta.fail_status = 401
        okta.answer_after = 1.0
        okta.start()
        try:
            directory("okta", base_url=okta.base_url)
            resp = client.get(f"/api/v1/auth/oidc/{PROVIDER}/callback")
        finally:
            okta.stop()

        assert resp.status_code == 401, resp.text
        assert app.state.account_store.get(SUBJECT).groups == []
