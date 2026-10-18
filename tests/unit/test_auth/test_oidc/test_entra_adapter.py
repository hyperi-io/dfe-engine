#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_oidc/test_entra_adapter.py
#  Purpose:      Tests for Entra ID Graph API group adapter
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Tests for EntraAdapter.

All tests exercise code paths that do NOT require real Entra credentials.
The "no credentials" paths cover the majority of production safety
logic (fail-open fallback behaviour).

Tests that require a live Entra / Microsoft Graph endpoint are marked
``@pytest.mark.skip(reason="Needs Entra sandbox")``.
"""

import asyncio
import json
import threading
from collections.abc import Callable
from typing import Any

import pytest
import requests
from requests.adapters import BaseAdapter

from dfe_engine.auth.groups import GroupStore
from dfe_engine.auth.oidc.adapters import entra as entra_module
from dfe_engine.auth.oidc.adapters.base import (
    DirectoryCredentialError,
    DirectoryNotConfiguredError,
    LookupFailure,
)
from dfe_engine.auth.oidc.adapters.entra import EntraAdapter, graph_credentials
from dfe_engine.auth.oidc.models import GroupResolutionConfig, OIDCProvider
from dfe_engine.auth.oidc.registry import OIDCProviderRegistry
from dfe_engine.auth.oidc.sync import sync_provider
from tests.support.event_loop import HELD_SECONDS, ticks_while_held
from tests.unit.test_auth.test_oidc.entra_adapter_cases import (
    GRAPH_CREDENTIALS_CASES,
    GraphCredentialsCase,
)
from tests.unit.test_auth.test_oidc.local_graph import LocalGraph
from tests.unit.test_auth.test_oidc.unreachable_store import UnreachableSecretStore

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_provider(
    *,
    client_id_env: str = "ENTRA_CLIENT_ID",
    tenant_id_env: str = "ENTRA_TENANT_ID",
    client_secret_env: str = "ENTRA_CLIENT_SECRET",  # noqa: S107 — env var name, not a secret
) -> OIDCProvider:
    """Return an Entra OIDCProvider with the given env var names."""
    return OIDCProvider(
        type="entra_id",
        issuer="https://login.microsoftonline.com/test-tenant/v2.0",
        client_id_env=client_id_env,
        groups=GroupResolutionConfig(
            mode="api",
            tenant_id_env=tenant_id_env,
            client_secret_env=client_secret_env,
        ),
    )


class TestGraphCredentials:
    @pytest.mark.parametrize(
        "case", GRAPH_CREDENTIALS_CASES, ids=[case["id"] for case in GRAPH_CREDENTIALS_CASES]
    )
    def test_matches_expected(self, monkeypatch: pytest.MonkeyPatch, case: GraphCredentialsCase):
        for name, value in case["env"].items():
            monkeypatch.setenv(name, value)
        credentials = graph_credentials(provider=case["provider"], secrets=None)
        assert credentials == case["expected_credentials"]


# ---------------------------------------------------------------------------
# Constructor
# ---------------------------------------------------------------------------


class TestEntraAdapterConstructor:
    def test_stores_provider_reference(self):
        provider = _make_provider()
        adapter = EntraAdapter(provider)
        assert adapter._provider is provider

    def test_provider_type_is_entra_id(self):
        provider = _make_provider()
        adapter = EntraAdapter(provider)
        assert adapter._provider.type == "entra_id"

    def test_reads_env_var_names_from_provider(self):
        provider = _make_provider(
            client_id_env="MY_CLIENT_ID",
            tenant_id_env="MY_TENANT_ID",
            client_secret_env="MY_CLIENT_SECRET",
        )
        adapter = EntraAdapter(provider)
        assert adapter._provider.client_id_env == "MY_CLIENT_ID"
        assert adapter._provider.groups.tenant_id_env == "MY_TENANT_ID"
        assert adapter._provider.groups.client_secret_env == "MY_CLIENT_SECRET"

    def test_graph_base_constant(self):
        assert EntraAdapter.GRAPH_BASE == "https://graph.microsoft.com/v1.0"


# ---------------------------------------------------------------------------
# _get_token — no credentials available
# ---------------------------------------------------------------------------


class TestGetTokenMissingCredentials:
    def test_returns_none_when_no_env_vars_set(self, monkeypatch):
        # Ensure none of the env vars are present
        monkeypatch.delenv("ENTRA_CLIENT_ID", raising=False)
        monkeypatch.delenv("ENTRA_TENANT_ID", raising=False)
        monkeypatch.delenv("ENTRA_CLIENT_SECRET", raising=False)

        provider = _make_provider()
        adapter = EntraAdapter(provider)
        result = adapter._get_token()
        assert result is None

    def test_returns_none_when_client_id_env_var_missing(self, monkeypatch):
        monkeypatch.delenv("ENTRA_CLIENT_ID", raising=False)
        monkeypatch.setenv("ENTRA_TENANT_ID", "test-tenant-id")
        monkeypatch.setenv("ENTRA_CLIENT_SECRET", "test-secret")

        provider = _make_provider()
        adapter = EntraAdapter(provider)
        result = adapter._get_token()
        assert result is None

    def test_returns_none_when_tenant_id_env_var_missing(self, monkeypatch):
        monkeypatch.setenv("ENTRA_CLIENT_ID", "test-client-id")
        monkeypatch.delenv("ENTRA_TENANT_ID", raising=False)
        monkeypatch.setenv("ENTRA_CLIENT_SECRET", "test-secret")

        provider = _make_provider()
        adapter = EntraAdapter(provider)
        result = adapter._get_token()
        assert result is None

    def test_returns_none_when_client_secret_env_var_missing(self, monkeypatch):
        monkeypatch.setenv("ENTRA_CLIENT_ID", "test-client-id")
        monkeypatch.setenv("ENTRA_TENANT_ID", "test-tenant-id")
        monkeypatch.delenv("ENTRA_CLIENT_SECRET", raising=False)

        provider = _make_provider()
        adapter = EntraAdapter(provider)
        result = adapter._get_token()
        assert result is None

    def test_returns_none_when_env_var_name_is_empty_string(self, monkeypatch):
        # Provider configured with empty env var names
        provider = OIDCProvider(
            type="entra_id",
            issuer="https://login.microsoftonline.com/test/v2.0",
            client_id_env="",
            groups=GroupResolutionConfig(
                mode="api",
                tenant_id_env="",
                client_secret_env="",
            ),
        )
        adapter = EntraAdapter(provider)
        result = adapter._get_token()
        assert result is None


# ---------------------------------------------------------------------------
# resolve_user_groups (the >200 overage enrichment) -- no answer when no credentials
# ---------------------------------------------------------------------------


class TestResolveUserGroupsNoCredentials:
    async def test_answers_nothing_when_no_credentials(self, monkeypatch):
        monkeypatch.delenv("ENTRA_CLIENT_ID", raising=False)
        monkeypatch.delenv("ENTRA_TENANT_ID", raising=False)
        monkeypatch.delenv("ENTRA_CLIENT_SECRET", raising=False)

        adapter = EntraAdapter(_make_provider())
        # No credential configured is a refusal, never an outage and never a raise.
        result = await adapter.resolve_user_groups("some-user-oid")
        assert result is LookupFailure.REFUSED

    async def test_empty_directory_id_short_circuits(self, monkeypatch):
        monkeypatch.delenv("ENTRA_CLIENT_ID", raising=False)
        adapter = EntraAdapter(_make_provider())
        assert await adapter.resolve_user_groups("") == []


# ---------------------------------------------------------------------------
# list_all_groups — empty list when no credentials
# ---------------------------------------------------------------------------


class TestListAllGroupsNoCredentials:
    async def test_refuses_to_list_when_no_credentials(self, monkeypatch):
        monkeypatch.delenv("ENTRA_CLIENT_ID", raising=False)
        monkeypatch.delenv("ENTRA_TENANT_ID", raising=False)
        monkeypatch.delenv("ENTRA_CLIENT_SECRET", raising=False)

        adapter = EntraAdapter(_make_provider())
        with pytest.raises(DirectoryNotConfiguredError):
            await adapter.list_all_groups()

    @pytest.mark.parametrize(
        "missing", ["ENTRA_CLIENT_ID", "ENTRA_TENANT_ID", "ENTRA_CLIENT_SECRET"]
    )
    async def test_refuses_to_list_when_one_credential_is_missing(self, monkeypatch, missing):
        for name in ("ENTRA_CLIENT_ID", "ENTRA_TENANT_ID", "ENTRA_CLIENT_SECRET"):
            monkeypatch.setenv(name, "set")
        monkeypatch.delenv(missing)

        adapter = EntraAdapter(_make_provider())
        with pytest.raises(DirectoryNotConfiguredError):
            await adapter.list_all_groups()


# ---------------------------------------------------------------------------
# test_connection — returns (False, message) when no credentials
# ---------------------------------------------------------------------------


class TestTestConnectionNoCredentials:
    async def test_returns_false_when_no_credentials(self, monkeypatch):
        monkeypatch.delenv("ENTRA_CLIENT_ID", raising=False)
        monkeypatch.delenv("ENTRA_TENANT_ID", raising=False)
        monkeypatch.delenv("ENTRA_CLIENT_SECRET", raising=False)

        adapter = EntraAdapter(_make_provider())
        success, message = await adapter.test_connection()
        assert success is False

    async def test_returns_message_string_when_no_credentials(self, monkeypatch):
        monkeypatch.delenv("ENTRA_CLIENT_ID", raising=False)
        monkeypatch.delenv("ENTRA_TENANT_ID", raising=False)
        monkeypatch.delenv("ENTRA_CLIENT_SECRET", raising=False)

        adapter = EntraAdapter(_make_provider())
        success, message = await adapter.test_connection()
        assert isinstance(message, str)
        assert len(message) > 0

    async def test_returns_tuple_of_two_elements(self, monkeypatch):
        monkeypatch.delenv("ENTRA_CLIENT_ID", raising=False)
        monkeypatch.delenv("ENTRA_TENANT_ID", raising=False)
        monkeypatch.delenv("ENTRA_CLIENT_SECRET", raising=False)

        adapter = EntraAdapter(_make_provider())
        result = await adapter.test_connection()
        assert isinstance(result, tuple)
        assert len(result) == 2

    async def test_message_mentions_credentials(self, monkeypatch):
        monkeypatch.delenv("ENTRA_CLIENT_ID", raising=False)
        monkeypatch.delenv("ENTRA_TENANT_ID", raising=False)
        monkeypatch.delenv("ENTRA_CLIENT_SECRET", raising=False)

        adapter = EntraAdapter(_make_provider())
        _success, message = await adapter.test_connection()
        # Message should explain why it failed — credentials issue
        assert any(
            keyword in message.lower()
            for keyword in ("credential", "token", "configure", "missing", "env")
        )


class TestTestConnectionCredentialState:
    """A credential nobody set and one Entra will not issue a token for are different faults."""

    @pytest.mark.parametrize(
        "missing", ["ENTRA_CLIENT_ID", "ENTRA_TENANT_ID", "ENTRA_CLIENT_SECRET"]
    )
    async def test_a_credential_that_is_not_set_reads_as_not_configured(self, monkeypatch, missing):
        for name in ("ENTRA_CLIENT_ID", "ENTRA_TENANT_ID", "ENTRA_CLIENT_SECRET"):
            monkeypatch.setenv(name, "set")
        monkeypatch.delenv(missing)

        success, message = await EntraAdapter(_make_provider()).test_connection()

        assert success is False
        assert "not configured" in message
        assert "could not use the configured credential" not in message

    async def test_a_credential_that_is_set_but_gets_no_token_reads_as_unusable(self, monkeypatch):
        for name in ("ENTRA_CLIENT_ID", "ENTRA_TENANT_ID", "ENTRA_CLIENT_SECRET"):
            monkeypatch.setenv(name, "set")
        adapter = EntraAdapter(_make_provider())
        # MSAL pins an https authority at login.microsoftonline.com, so Entra's refusal
        # cannot come from a loopback server: it arrives as the token call returning None.
        adapter._get_token = lambda: None  # ty: ignore[invalid-assignment]  # the test replaces this adapter's token fetch

        success, message = await adapter.test_connection()

        assert success is False
        assert message == (
            "Graph API could not use the configured credential: check it is current "
            "and valid; the engine log has the reason"
        )
        assert "not configured" not in message


def _stored_secret_provider() -> OIDCProvider:
    """An api-mode Entra provider whose client secret is held only in the secret store."""
    return OIDCProvider(
        type="entra_id",
        issuer="https://login.microsoftonline.com/test-tenant/v2.0",
        client_id="app-id",
        groups=GroupResolutionConfig(
            client_secret_path="oidc/acme/groups_client_secret",
            mode="api",
            tenant_id="test-tenant",
        ),
    )


class TestSecretStoreDidNotAnswer:
    """A client secret whose secret store is down exists all the same: an error, not 'not configured'."""

    async def test_the_listing_fails_as_an_error_and_the_login_lookup_still_answers(self):
        adapter = EntraAdapter(_stored_secret_provider(), secrets=UnreachableSecretStore())

        with pytest.raises(DirectoryCredentialError):
            await adapter.list_all_groups()
        # The secret store not answering is an outage, so the login keeps its last groups.
        assert await adapter.resolve_user_groups("some-user-oid") is LookupFailure.OUTAGE

    async def test_the_connection_test_names_the_secret_store(self):
        adapter = EntraAdapter(_stored_secret_provider(), secrets=UnreachableSecretStore())

        ok, message = await adapter.test_connection()

        assert ok is False
        assert "secret store did not answer" in message
        assert "not configured" not in message


class TestEntraUnreachable:
    """A token request that got no answer blames the network, never the configured credential."""

    async def test_the_connection_test_says_entra_could_not_be_reached_and_logins_still_answer(
        self, entra_unreachable, audit_events, monkeypatch
    ):
        adapter = EntraAdapter(_make_provider())

        for name in ("ENTRA_CLIENT_ID", "ENTRA_TENANT_ID", "ENTRA_CLIENT_SECRET"):
            monkeypatch.setenv(name, "set")

        ok, message = await adapter.test_connection()

        assert ok is False
        assert "could not be reached" in message
        assert "could not use the configured credential" not in message
        (line,) = [e for e in audit_events if e.get("operation") == "token"]
        assert line["transport_failure"] == "connection_refused"
        assert await adapter.resolve_user_groups("some-user-oid") is LookupFailure.OUTAGE


_DISCOVERY_SUFFIX = "/v2.0/.well-known/openid-configuration"
_TOKEN_SUFFIX = "/oauth2/v2.0/token"


class _CannedEntra(BaseAdapter):
    """A requests transport standing in for Entra: discovery answers, the token request gets ``token_status``.

    MSAL reaches login.microsoftonline.com over https, which a loopback server cannot
    serve, so the answer is canned at the transport that MSAL's own HTTP client uses.
    A 200 token request answers ``access_token``. ``arrived`` is set when a token request
    reaches the transport, and the request then waits for ``gate`` before it answers,
    recording in ``gate_opened_by_test`` whether the test opened it or the wait timed out.
    """

    def __init__(
        self,
        *,
        token_status: int,
        access_token: str = "app-token",  # noqa: S107 - a canned answer, not a secret
        arrived: threading.Event | None = None,
        gate: threading.Event | None = None,
    ) -> None:
        super().__init__()
        self.access_token = access_token
        self.arrived = arrived
        self.gate = gate
        self.token_status = token_status
        self.gate_opened_by_test: list[bool] = []
        self.token_threads: list[int] = []
        self.token_timeouts: list[Any] = []
        self.urls: list[str] = []

    def send(
        self,
        request: requests.PreparedRequest,
        stream: bool = False,
        timeout: Any = None,
        verify: bool | str = True,
        cert: Any = None,
        proxies: Any = None,
    ) -> requests.Response:
        """Answer ``request`` from the canned settings, recording its URL."""
        url = request.url or ""
        self.urls.append(url)
        response = requests.Response()
        response.request = request
        response.url = url
        if url.endswith(_DISCOVERY_SUFFIX):
            tenant_root = url.removesuffix(_DISCOVERY_SUFFIX)
            body: dict[str, Any] = {
                "authorization_endpoint": f"{tenant_root}/oauth2/v2.0/authorize",
                "issuer": f"{tenant_root}/v2.0",
                "token_endpoint": f"{tenant_root}{_TOKEN_SUFFIX}",
            }
            response.status_code = 200
        else:
            self.token_threads.append(threading.get_ident())
            self.token_timeouts.append(timeout)
            if self.arrived is not None:
                self.arrived.set()
            if self.gate is not None:
                self.gate_opened_by_test.append(self.gate.wait(timeout=HELD_SECONDS))
            response.status_code = self.token_status
            if self.token_status == 200:
                body = {
                    "access_token": self.access_token,
                    "expires_in": 3600,
                    "token_type": "Bearer",
                }
            else:
                body = {"error": "temporarily_unavailable", "error_description": "throttled"}
                response.headers["Retry-After"] = "1"
        response.headers["Content-Type"] = "application/json"
        response._content = json.dumps(body).encode("utf-8")
        return response

    def close(self) -> None:
        """Hold nothing to release."""

    @property
    def token_requests(self) -> int:
        """How many requests went to the token endpoint."""
        return sum(url.endswith(_TOKEN_SUFFIX) for url in self.urls)

    @property
    def discovery_requests(self) -> int:
        """How many requests went to the authority's discovery document."""
        return sum(url.endswith(_DISCOVERY_SUFFIX) for url in self.urls)


@pytest.fixture
def canned_entra(monkeypatch: pytest.MonkeyPatch) -> Callable[[_CannedEntra], None]:
    """The function that points MSAL's HTTP at a canned transport for the test."""

    def install(transport: _CannedEntra) -> None:
        def client() -> Any:
            session = requests.Session()
            session.mount("https://", transport)
            return entra_module._EntraHttpClient(session)

        monkeypatch.setattr(entra_module, "_new_http_client", client)

    return install


@pytest.fixture
def entra_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the three env vars the test provider reads its Entra credentials from."""
    for name in ("ENTRA_CLIENT_ID", "ENTRA_TENANT_ID", "ENTRA_CLIENT_SECRET"):
        monkeypatch.setenv(name, "set")


class TestEntraThrottlesOrFails:
    """A 429 or a 5xx from Entra is a transient failure, never a refused credential."""

    @pytest.mark.parametrize(
        ("status", "words"),
        [
            (429, "Entra ID is throttling requests (HTTP 429)"),
            (503, "Entra ID failed on the provider's side (HTTP 503)"),
        ],
        ids=["throttled", "failing"],
    )
    async def test_the_connection_test_names_the_status_not_the_credential(
        self, canned_entra, entra_credentials, entra_unreachable, status, words
    ):
        transport = _CannedEntra(token_status=status)
        canned_entra(transport)

        ok, message = await EntraAdapter(_make_provider()).test_connection()

        assert transport.token_requests == 1
        assert ok is False
        assert words in message
        assert "could not use the configured credential" not in message

    async def test_a_throttled_sync_is_a_failed_run_that_says_so(
        self, canned_entra, entra_credentials, entra_unreachable, tmp_path
    ):
        registry = OIDCProviderRegistry(tmp_path / "oidc")
        registry.create("entra", _make_provider())
        transport = _CannedEntra(token_status=429)
        canned_entra(transport)

        result = await sync_provider(
            "entra",
            registry,
            GroupStore(tmp_path / "groups"),
            adapter=EntraAdapter(_make_provider()),
        )

        stored = registry.get("entra")
        assert stored is not None
        assert transport.token_requests == 1
        assert (result["status"], stored.last_sync_status) == ("error", "error")
        # No token was issued, so Graph was never asked: the directory API is not the one throttling.
        assert result["error"] == (
            "Entra ID is throttling token requests (HTTP 429); retry shortly"
        )
        assert stored.sync_error == result["error"]


class TestEntraTokenRequest:
    """The token request is blocking HTTP, so it runs off the event loop and its client is kept."""

    def test_a_second_adapter_for_the_same_registration_reuses_the_token(
        self, canned_entra, entra_credentials
    ):
        transport = _CannedEntra(token_status=200)
        canned_entra(transport)

        first = EntraAdapter(_make_provider())._get_token()
        second = EntraAdapter(_make_provider())._get_token()

        assert (first, second) == ("app-token", "app-token")
        assert (transport.discovery_requests, transport.token_requests) == (1, 1)

    def test_a_rotated_secret_asks_entra_again(self, canned_entra, entra_credentials, monkeypatch):
        transport = _CannedEntra(token_status=200)
        canned_entra(transport)
        EntraAdapter(_make_provider())._get_token()

        monkeypatch.setenv("ENTRA_CLIENT_SECRET", "rotated")
        EntraAdapter(_make_provider())._get_token()

        assert (transport.discovery_requests, transport.token_requests) == (2, 2)

    def test_a_token_request_has_a_deadline(self, canned_entra, entra_credentials):
        """MSAL's own default waits for Entra without limit, which would hold a worker thread for good."""
        transport = _CannedEntra(token_status=200)
        canned_entra(transport)

        EntraAdapter(_make_provider())._get_token()

        assert transport.token_timeouts == [entra_module._MSAL_TIMEOUT_SECONDS]

    async def test_a_slow_token_request_does_not_block_the_event_loop(
        self, canned_entra, entra_credentials, graph
    ):
        arrived, gate = threading.Event(), threading.Event()
        transport = _CannedEntra(token_status=200, arrived=arrived, gate=gate)
        canned_entra(transport)
        graph.pages["groups"] = [[ADMINS]]
        adapter = EntraAdapter(_make_provider())
        adapter.GRAPH_BASE = graph.base_url

        listing = asyncio.create_task(adapter.list_all_groups())
        ticks = await ticks_while_held(arrived=arrived, gate=gate)
        groups = await listing

        # The event loop ran while the request was held: the test opened the gate, the wait did not time out.
        assert transport.gate_opened_by_test == [True]
        assert ticks >= 5
        assert [g.name for g in groups] == ["dfe-admins"]
        assert threading.get_ident() not in transport.token_threads


# ---------------------------------------------------------------------------
# Live Entra tests (skipped — require sandbox)
# ---------------------------------------------------------------------------


@pytest.mark.skip(reason="Needs Entra sandbox")
class TestEntraAdapterLive:
    async def test_list_all_groups_returns_group_info_objects(self):
        """Verify list_all_groups returns GroupInfo with id and name."""
        from dfe_engine.auth.oidc.models import GroupInfo

        provider = _make_provider()
        adapter = EntraAdapter(provider)
        groups = await adapter.list_all_groups()
        assert len(groups) > 0
        for g in groups:
            assert isinstance(g, GroupInfo)
            assert g.id
            assert g.name

    async def test_test_connection_returns_true_with_valid_credentials(self):
        """Verify connection test succeeds with valid Entra credentials."""
        provider = _make_provider()
        adapter = EntraAdapter(provider)
        success, message = await adapter.test_connection()
        assert success is True
        assert message


# ---------------------------------------------------------------------------
# Overage: the user's own token first, then the app credentials
# ---------------------------------------------------------------------------

OID = "00000000-0000-4000-8000-0000000000aa"
ADMINS = {"id": "11111111-0000-4000-8000-000000000001", "displayName": "dfe-admins"}
VIEWERS = {"id": "11111111-0000-4000-8000-000000000002", "displayName": "dfe-viewers"}
USER_TOKEN = "user-token"
SCOPE_REFUSED = {
    "error": {"code": "Authorization_RequestDenied", "message": "Insufficient privileges"}
}


@pytest.fixture
def graph():
    server = LocalGraph()
    server.start()
    yield server
    server.stop()


def _overage_adapter(
    server: LocalGraph, *, token: str = USER_TOKEN, app_token: str | None = None
) -> EntraAdapter:
    """An Entra adapter reading Graph at *server*; *app_token* stands in for the client-credentials token."""
    adapter = EntraAdapter(_make_provider(), access_token=token)
    adapter.GRAPH_BASE = server.base_url
    adapter._get_token = lambda: app_token  # ty: ignore[invalid-assignment]  # the test replaces this adapter's token fetch
    return adapter


class TestOverageUserToken:
    async def test_the_users_own_token_reads_their_groups(self, graph):
        graph.pages["me"] = [[ADMINS, VIEWERS]]
        groups = await _overage_adapter(graph).resolve_user_groups(OID)

        assert isinstance(groups, list)
        assert [g.name for g in groups] == ["dfe-admins", "dfe-viewers"]
        assert graph.principals == ["me"]
        assert graph.requests[0]["authorization"] == "Bearer user-token"

    async def test_a_refused_user_token_falls_back_to_the_app_credentials(self, graph):
        graph.refuse["me"] = (403, SCOPE_REFUSED)
        graph.pages[f"users/{OID}"] = [[VIEWERS]]
        groups = await _overage_adapter(graph, app_token="app-token").resolve_user_groups(OID)

        assert isinstance(groups, list)
        assert [g.name for g in groups] == ["dfe-viewers"]
        assert graph.principals == ["me", f"users/{OID}"]
        assert graph.requests[1]["authorization"] == "Bearer app-token"

    async def test_no_groups_for_the_user_is_the_answer(self, graph):
        groups = await _overage_adapter(graph, app_token="app-token").resolve_user_groups(OID)

        assert groups == []
        assert graph.principals == ["me"]

    async def test_without_a_user_token_the_app_credentials_answer(self, graph):
        graph.pages[f"users/{OID}"] = [[ADMINS]]
        groups = await _overage_adapter(graph, token="", app_token="app-token").resolve_user_groups(
            OID
        )

        assert isinstance(groups, list)
        assert [g.name for g in groups] == ["dfe-admins"]
        assert graph.principals == [f"users/{OID}"]

    async def test_refused_everywhere_is_a_refusal(self, graph):
        graph.refuse["me"] = (403, SCOPE_REFUSED)
        graph.refuse[f"users/{OID}"] = (403, SCOPE_REFUSED)
        groups = await _overage_adapter(graph, app_token="app-token").resolve_user_groups(OID)
        assert groups is LookupFailure.REFUSED

    @pytest.mark.parametrize("status", [429, 500, 503])
    async def test_graph_failing_on_both_paths_is_an_outage(self, graph, status):
        graph.refuse["me"] = (status, {"error": {"code": "ServiceUnavailable"}})
        graph.refuse[f"users/{OID}"] = (status, {"error": {"code": "ServiceUnavailable"}})
        groups = await _overage_adapter(graph, app_token="app-token").resolve_user_groups(OID)
        assert groups is LookupFailure.OUTAGE

    async def test_a_refused_user_token_and_a_failing_app_path_is_an_outage(self, graph):
        """The app credentials could have answered, so their outage is the result."""
        graph.refuse["me"] = (403, SCOPE_REFUSED)
        graph.refuse[f"users/{OID}"] = (503, {"error": {"code": "ServiceUnavailable"}})
        groups = await _overage_adapter(graph, app_token="app-token").resolve_user_groups(OID)
        assert groups is LookupFailure.OUTAGE

    async def test_every_page_is_read(self, graph):
        graph.pages["me"] = [[ADMINS], [VIEWERS]]
        groups = await _overage_adapter(graph).resolve_user_groups(OID)

        assert isinstance(groups, list)
        assert [g.name for g in groups] == ["dfe-admins", "dfe-viewers"]
        assert graph.principals == ["me", "me"]

    def test_the_factory_hands_entra_the_login_token(self):
        from dfe_engine.auth.oidc.adapters import get_adapter

        adapter = get_adapter(_make_provider(), access_token="login-token")
        assert isinstance(adapter, EntraAdapter)
        assert adapter._access_token == "login-token"

    async def test_groups_the_token_may_not_read_still_resolve_by_id(self, graph):
        """Graph answers a User.Read token with each group's id and every other property null."""
        graph.pages["me"] = [[ADMINS, VIEWERS]]
        graph.limited.add("me")
        groups = await _overage_adapter(graph, app_token="app-token").resolve_user_groups(OID)

        assert isinstance(groups, list)
        assert [g.id for g in groups] == [ADMINS["id"], VIEWERS["id"]]
        assert [g.name for g in groups] == ["", ""]
        assert graph.principals == ["me"]

    async def test_a_failure_partway_through_is_no_answer_not_a_partial_one(self, graph):
        graph.pages["me"] = [[ADMINS], [VIEWERS]]
        graph.fail_page["me"] = 1
        graph.pages[f"users/{OID}"] = [[ADMINS, VIEWERS]]
        groups = await _overage_adapter(graph, app_token="app-token").resolve_user_groups(OID)

        assert isinstance(groups, list)
        assert [g.name for g in groups] == ["dfe-admins", "dfe-viewers"]
        assert graph.principals == ["me", "me", f"users/{OID}"]
