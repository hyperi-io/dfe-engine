#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_oidc/test_idp_error_redaction.py
#  Purpose:      A failed IdP call logs its status and code, never the user it was about
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""A failed identity provider call logs its status and error code, never the user it named.

Each test makes a real call to a local stand-in that refuses it. The request URL
or the refusal body carries :data:`SENTINEL`, as Google's ``userKey=<email>``,
Graph's ``/users/<upn>`` and Okta's ``/users/<login>`` do. The test then reads every
line the engine's logger wrote and checks the sentinel is absent, in any encoding,
while the status and the provider's code are present.
"""

import json
import socket
import ssl
from typing import Any

import httpx
import pytest
from authlib.integrations.base_client import MismatchingStateError, OAuthError
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from google.auth.credentials import AnonymousCredentials
from google.auth.exceptions import RefreshError
from googleapiclient.discovery import build

from dfe_engine.auth.groups import GroupStore
from dfe_engine.auth.oidc.adapters.base import OIDCGroupAdapter
from dfe_engine.auth.oidc.adapters.entra import EntraAdapter
from dfe_engine.auth.oidc.adapters.google import GoogleAdapter
from dfe_engine.auth.oidc.adapters.okta import OktaAdapter
from dfe_engine.auth.oidc.models import GroupInfo, GroupResolutionConfig, OIDCProvider
from dfe_engine.auth.oidc.registry import OIDCProviderRegistry
from dfe_engine.auth.oidc.rp import fill_from_userinfo_endpoint
from dfe_engine.auth.oidc.sync import sync_provider
from dfe_engine.http_errors import (
    describe_http_error,
    http_failure_message,
    provider_error_code,
)
from tests.support.refusing_api import refusing_api
from tests.unit.test_auth.factories import make_oauth_client
from tests.unit.test_auth.test_oidc.local_cloud_identity import LocalCloudIdentity
from tests.unit.test_auth.test_oidc.local_graph import TENANT, LocalGraph
from tests.unit.test_auth.test_oidc.local_idp import LocalIdp
from tests.unit.test_auth.test_oidc.local_okta import LocalOkta

SENTINEL = "idp.sentinel.7f3a@example.com"
# The local part survives every encoding a URL or a repr gives the address, so it is what must not appear.
SENTINEL_LOCAL_PART = "idp.sentinel.7f3a"


def assert_no_sentinel(text: str) -> None:
    """``text`` names the sentinel user in no form."""
    assert SENTINEL_LOCAL_PART not in text, text


def failure_line(lines: list[dict], operation: str) -> dict:
    """The one log line that reports a failed ``operation``."""
    (line,) = [line for line in lines if line.get("operation") == operation]
    return line


def logged(lines: list[dict]) -> str:
    """Every captured line as one string, for a substring check."""
    return json.dumps(lines, default=str)


def _caused_by(outer: BaseException, cause: BaseException) -> BaseException:
    """``outer`` raised from ``cause``, the shape an HTTP client gives a socket error."""
    outer.__cause__ = cause
    return outer


# ---------------------------------------------------------------------------
# The helper itself
# ---------------------------------------------------------------------------


class TestProviderErrorCode:
    @pytest.mark.parametrize(
        ("body", "expected"),
        [
            (
                {"error": {"code": "Request_ResourceNotFound", "message": SENTINEL}},
                "Request_ResourceNotFound",
            ),
            (
                {
                    "error": {
                        "code": 403,
                        "details": [{"reason": "SERVICE_DISABLED"}],
                        "status": "PERMISSION_DENIED",
                    }
                },
                "PERMISSION_DENIED;SERVICE_DISABLED",
            ),
            (
                {"error": {"code": 404, "errors": [{"message": SENTINEL, "reason": "notFound"}]}},
                "notFound",
            ),
            (
                {"errorCode": "E0000007", "errorSummary": f"Not found: {SENTINEL} (User)"},
                "E0000007",
            ),
            ({"error": "invalid_client", "error_description": SENTINEL}, "invalid_client"),
            ({"error": {"code": f"no such user {SENTINEL}"}}, ""),
            ({"error": "invalid_client\n"}, ""),
            (["not", "an", "object"], ""),
            (None, ""),
        ],
        ids=[
            "graph",
            "google-cloud",
            "google-directory",
            "okta",
            "oauth",
            "prose-in-code",
            "trailing-newline",
            "list",
            "none",
        ],
    )
    def test_reads_only_the_code(self, body: object, expected: str) -> None:
        assert provider_error_code(body) == expected

    def test_a_failure_with_no_answer_is_named_by_its_class(self) -> None:
        assert describe_http_error(ConnectionError(f"cannot reach {SENTINEL}")) == {
            "error_type": "ConnectionError"
        }

    @pytest.mark.parametrize(
        ("exc", "expected"),
        [
            (_caused_by(httpx.ConnectError("x"), socket.gaierror(-2, "unknown name")), "dns"),
            (_caused_by(httpx.ConnectError("x"), ssl.SSLCertVerificationError("bad")), "tls"),
            (_caused_by(httpx.ConnectError("x"), ConnectionRefusedError()), "connection_refused"),
            (_caused_by(httpx.ConnectError("x"), ConnectionResetError()), "connection_reset"),
            (_caused_by(httpx.ConnectError("x"), TimeoutError()), "timeout"),
            (httpx.ConnectTimeout("x"), "timeout"),
            (httpx.PoolTimeout("x"), "timeout"),
        ],
        ids=[
            "dns",
            "tls",
            "refused",
            "reset",
            "socket-timeout",
            "connect-timeout",
            "pool-timeout",
        ],
    )
    def test_a_failure_with_no_answer_says_why_by_the_socket_error_class(
        self, exc: BaseException, expected: str
    ) -> None:
        assert describe_http_error(exc) == {
            "error_type": type(exc).__name__,
            "transport_failure": expected,
        }

    def test_a_cause_named_only_in_the_message_is_not_read(self) -> None:
        """The class decides: a message that mentions DNS, TLS or a timeout says nothing."""
        message = f"dns lookup of {SENTINEL} failed: tls handshake timed out, connection refused"

        assert describe_http_error(ConnectionError(message)) == {"error_type": "ConnectionError"}

    def test_an_oauth_error_yields_its_code_and_not_its_description(self) -> None:
        """Authlib builds the error from the callback's query string, so the description is the caller's."""
        error = OAuthError(error="access_denied", description=f"{SENTINEL} may not sign in")

        assert describe_http_error(error) == {"code": "access_denied", "error_type": "OAuthError"}

    @pytest.mark.parametrize(
        ("exc", "code"),
        [
            (OAuthError(error="login_required"), "login_required"),
            (OAuthError(error="invalid_grant"), "invalid_grant"),
            (OAuthError(error="attacker_chosen.reason-1"), "unrecognised"),
            (MismatchingStateError(), "mismatching_state"),
            (OAuthError(description='Missing "state" parameter'), "oauth_error"),
        ],
        ids=[
            "oidc-code",
            "token-endpoint-code",
            "caller-chosen",
            "library-class",
            "library-default",
        ],
    )
    def test_an_oauth_code_outside_the_standards_is_unrecognised(
        self, exc: OAuthError, code: str
    ) -> None:
        """A callback's caller chooses the code, so only the specs' codes and the library's own pass."""
        assert describe_http_error(exc)["code"] == code

    @pytest.mark.parametrize(
        "exc",
        [
            json.JSONDecodeError("Expecting value", "<html>", 0),
            RuntimeError(f"unexpected answer about {SENTINEL}"),
        ],
        ids=["body-not-json", "no-socket-error"],
    )
    def test_an_answer_that_arrived_is_not_called_unreachable(self, exc: BaseException) -> None:
        message = http_failure_message(exc, service="The issuer's discovery endpoint")

        assert "could not be reached" not in message
        assert message == (
            "The issuer's discovery endpoint gave no usable answer; the engine log has the reason"
        )

    def test_a_failure_with_a_socket_error_under_it_is_unreachable(self) -> None:
        exc = _caused_by(httpx.ConnectError("x"), ConnectionRefusedError())

        message = http_failure_message(exc, service="The directory API")

        assert message == "The directory API could not be reached; the engine log has the reason"


# ---------------------------------------------------------------------------
# Entra: Graph /users/<id> answers 404
# ---------------------------------------------------------------------------

GRAPH_NOT_FOUND = {
    "error": {
        "code": "Request_ResourceNotFound",
        "message": f"Resource '{SENTINEL}' does not exist or one of its queried "
        "reference-property objects are not present.",
    }
}


class _AppTokenEntra(EntraAdapter):
    """Entra with its client-credentials token already in hand, so no MSAL call is made."""

    def _get_token(self) -> str | None:
        return "app-token"


def _entra_adapter(base_url: str) -> EntraAdapter:
    """An Entra adapter with no user token that reads Graph at ``base_url`` with an app token."""
    provider = OIDCProvider(
        type="entra_id",
        issuer="https://login.microsoftonline.com/test-tenant/v2.0",
        groups=GroupResolutionConfig(mode="api"),
    )
    adapter = _AppTokenEntra(provider)
    adapter.GRAPH_BASE = base_url
    return adapter


class TestEntra:
    async def test_a_refused_user_lookup_logs_the_status_and_code_not_the_user(
        self, audit_events: list[dict]
    ) -> None:
        graph = LocalGraph()
        graph.refuse[f"users/{SENTINEL}"] = (404, GRAPH_NOT_FOUND)
        graph.start()
        try:
            groups = await _entra_adapter(graph.base_url).resolve_user_groups(SENTINEL)
        finally:
            graph.stop()

        assert groups == []
        assert graph.principals == [f"users/{SENTINEL}"]
        assert_no_sentinel(logged(audit_events))
        line = failure_line(audit_events, "resolve_user_groups")
        assert (line["status"], line["code"], line["error_type"]) == (
            404,
            "Request_ResourceNotFound",
            "HTTPStatusError",
        )

    async def test_a_refused_connection_test_answers_without_the_providers_text(
        self, audit_events: list[dict]
    ) -> None:
        refusal = {"error": {"code": "Authorization_RequestDenied", "message": SENTINEL}}
        with refusing_api(403, refusal) as server:
            ok, message = await _entra_adapter(server.base_url).test_connection()

        assert ok is False
        assert server.base_url not in message
        assert server.base_url not in logged(audit_events)
        assert_no_sentinel(message)
        assert "HTTP 403" in message
        line = failure_line(audit_events, "test_connection")
        assert (line["status"], line["code"]) == (403, "Authorization_RequestDenied")


# ---------------------------------------------------------------------------
# Okta: /users/<login>/groups answers 404
# ---------------------------------------------------------------------------

OKTA_NOT_FOUND = {
    "errorCauses": [],
    "errorCode": "E0000007",
    "errorId": "oae3iIi_z_SR0yMf5TFpTsxbg",
    "errorLink": "E0000007",
    "errorSummary": f"Not found: Resource not found: {SENTINEL} (User)",
}


class _PlainHttpOkta(OktaAdapter):
    """Okta whose Management API is a plain-http stand-in; the adapter builds only https origins."""

    def __init__(self, provider: OIDCProvider, *, base_url: str) -> None:
        super().__init__(provider)
        self._base_url = base_url

    def _api_base_and_headers(self) -> tuple[str | None, dict[str, str] | None]:
        return self._base_url, {"Accept": "application/json", "Authorization": "SSWS test-token"}


def _okta_adapter(base_url: str) -> OktaAdapter:
    """An api-mode Okta adapter whose Management API is at ``base_url``."""
    provider = OIDCProvider(
        type="okta",
        issuer="https://example.okta.com",
        groups=GroupResolutionConfig(mode="api", okta_domain="example.okta.com"),
    )
    return _PlainHttpOkta(provider, base_url=base_url)


class TestOkta:
    async def test_a_refused_user_lookup_logs_the_status_and_code_not_the_user(
        self, audit_events: list[dict]
    ) -> None:
        with refusing_api(404, OKTA_NOT_FOUND) as server:
            groups = await _okta_adapter(server.base_url).resolve_user_groups(SENTINEL)

        assert groups == []
        assert server.paths == [f"/users/{SENTINEL}/groups?limit=200"]
        assert_no_sentinel(logged(audit_events))
        line = failure_line(audit_events, "resolve_user_groups")
        assert (line["status"], line["code"], line["error_type"]) == (
            404,
            "E0000007",
            "HTTPStatusError",
        )

    async def test_a_refused_connection_test_answers_without_the_providers_text(
        self, audit_events: list[dict]
    ) -> None:
        with refusing_api(403, {"errorCode": "E0000006", "errorSummary": SENTINEL}) as server:
            ok, message = await _okta_adapter(server.base_url).test_connection()

        assert ok is False
        assert server.base_url not in message
        assert_no_sentinel(message)
        assert "HTTP 403" in message
        line = failure_line(audit_events, "test_connection")
        assert (line["status"], line["code"]) == (403, "E0000006")


# ---------------------------------------------------------------------------
# Google: the Directory API as the service account, Cloud Identity as the user
# ---------------------------------------------------------------------------

DIRECTORY_NOT_FOUND = {
    "error": {
        "code": 404,
        "errors": [{"domain": "global", "message": "Resource Not Found", "reason": "notFound"}],
        "message": f"Resource Not Found: {SENTINEL}",
    }
}


class _LocalDirectoryGoogle(GoogleAdapter):
    """Google whose Directory API client is built against a local stand-in."""

    def __init__(self, provider: OIDCProvider, *, base_url: str) -> None:
        super().__init__(provider)
        self._service = build(
            "admin",
            "directory_v1",
            cache_discovery=False,
            client_options={"api_endpoint": f"{base_url}/"},
            credentials=AnonymousCredentials(),
        )

    def _get_service(self) -> Any | None:
        return self._service


def _directory_adapter(base_url: str) -> GoogleAdapter:
    """A Google adapter with no user token whose service account reads the Directory API at ``base_url``."""
    provider = OIDCProvider(
        type="google",
        issuer="https://accounts.google.com",
        groups=GroupResolutionConfig(service_account_json_env="DFE_TEST_IDP_SA_JSON"),
    )
    return _LocalDirectoryGoogle(provider, base_url=base_url)


TOKEN_REFUSED = {
    "error": "invalid_grant",
    "error_description": f"Invalid JWT Signature for {SENTINEL}",
}


@pytest.fixture(scope="module")
def service_account_pem() -> str:
    """A throwaway RSA key in the PEM form a service account JSON carries."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")


def _token_refusing_adapter(
    *, monkeypatch: pytest.MonkeyPatch, pem: str, token_uri: str
) -> GoogleAdapter:
    """A Google adapter whose own service account client posts its token request to ``token_uri``."""
    service_account = {
        "client_email": "dfe-test@example-project.iam.gserviceaccount.com",
        "private_key": pem,
        "private_key_id": "0123456789abcdef",
        "token_uri": token_uri,
        "type": "service_account",
    }
    monkeypatch.setenv("DFE_TEST_IDP_SA_JSON", json.dumps(service_account))
    provider = OIDCProvider(
        type="google",
        issuer="https://accounts.google.com",
        groups=GroupResolutionConfig(mode="api", service_account_json_env="DFE_TEST_IDP_SA_JSON"),
    )
    return GoogleAdapter(provider)


class TestGoogleDirectory:
    async def test_a_refused_user_lookup_logs_the_status_and_code_not_the_user(
        self, audit_events: list[dict]
    ) -> None:
        with refusing_api(404, DIRECTORY_NOT_FOUND) as server:
            groups = await _directory_adapter(server.base_url).resolve_user_groups(SENTINEL)

        assert groups == []
        # googleapiclient percent-encodes the address, a form the log scrubber does not recognise.
        assert "userKey=idp.sentinel.7f3a%40example.com" in server.paths[0]
        assert_no_sentinel(logged(audit_events))
        line = failure_line(audit_events, "resolve_user_groups")
        assert (line["status"], line["code"], line["error_type"]) == (404, "notFound", "HttpError")

    async def test_a_refused_connection_test_answers_without_the_providers_text(
        self, audit_events: list[dict], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DFE_TEST_IDP_SA_JSON", '{"type": "service_account"}')
        refusal = {
            "error": {
                "code": 403,
                "errors": [{"reason": "forbidden"}],
                "message": f"Not Authorized: {SENTINEL}",
            }
        }
        with refusing_api(403, refusal) as server:
            ok, message = await _directory_adapter(server.base_url).test_connection()

        assert ok is False
        assert server.base_url not in message
        assert_no_sentinel(message)
        assert "HTTP 403" in message
        line = failure_line(audit_events, "test_connection")
        assert (line["status"], line["code"]) == (403, "forbidden")

    async def test_a_token_refusal_blames_the_credential_not_the_network(
        self,
        audit_events: list[dict],
        monkeypatch: pytest.MonkeyPatch,
        service_account_pem: str,
    ) -> None:
        with refusing_api(400, TOKEN_REFUSED) as token_endpoint:
            adapter = _token_refusing_adapter(
                monkeypatch=monkeypatch,
                pem=service_account_pem,
                token_uri=f"{token_endpoint.base_url}/token",
            )
            ok, message = await adapter.test_connection()

        assert token_endpoint.paths == ["/token"]
        assert ok is False
        assert "could not use the configured credential" in message
        assert "could not be reached" not in message
        assert token_endpoint.base_url not in message
        assert_no_sentinel(message)
        assert_no_sentinel(logged(audit_events))
        line = failure_line(audit_events, "test_connection")
        assert (line["error_type"], line["code"]) == ("RefreshError", "invalid_grant")


class TestGoogleTokenRefusalClass:
    """Which ``RefreshError`` google-auth raises is a refusal of the credential, and what it logs."""

    @pytest.mark.parametrize(
        ("exc", "blames_credential"),
        [
            pytest.param(
                RefreshError("invalid_grant: x", TOKEN_REFUSED, retryable=False),
                True,
                id="refused",
            ),
            pytest.param(RefreshError("an HTML page from a proxy"), True, id="no-json-answer"),
            pytest.param(
                RefreshError("server_error: x", {"error": "server_error"}, retryable=True),
                False,
                id="provider-failing-retryable",
            ),
        ],
    )
    def test_only_a_refusal_the_provider_will_repeat_blames_the_credential(
        self, exc: RefreshError, blames_credential: bool
    ) -> None:
        message = http_failure_message(exc, service="The directory API")

        assert ("could not use the configured credential" in message) is blames_credential
        # The token endpoint answered either way, so neither reads as unreachable.
        assert "could not be reached" not in message

    def test_the_log_fields_carry_the_oauth_code_not_its_description(self) -> None:
        exc = RefreshError(f"invalid_grant: {SENTINEL}", TOKEN_REFUSED)

        assert describe_http_error(exc) == {"error_type": "RefreshError", "code": "invalid_grant"}

    def test_an_answer_that_is_not_json_yields_no_code(self) -> None:
        assert describe_http_error(RefreshError("an HTML page from a proxy")) == {
            "error_type": "RefreshError"
        }


class TestGoogleCloudIdentity:
    async def test_a_refused_search_logs_googles_code_not_its_message(
        self, audit_events: list[dict]
    ) -> None:
        refusal = {
            "error": {
                "code": 403,
                "details": [{"reason": "ACCESS_TOKEN_SCOPE_INSUFFICIENT"}],
                "message": f"Permission denied for member {SENTINEL}",
                "status": "PERMISSION_DENIED",
            }
        }
        server = LocalCloudIdentity()
        server.refuse["searchTransitiveGroups"] = (403, refusal)
        server.refuse["searchDirectGroups"] = (403, refusal)
        server.start()
        try:
            adapter = GoogleAdapter(
                OIDCProvider(type="google", issuer="https://accounts.google.com"),
                access_token="user-token",
            )
            adapter.CLOUD_IDENTITY_BASE = server.base_url
            groups = await adapter.resolve_user_groups(SENTINEL)
        finally:
            server.stop()

        assert groups == []
        assert_no_sentinel(logged(audit_events))
        line = failure_line(audit_events, "searchDirectGroups")
        assert (line["status"], line["code"]) == (
            403,
            "PERMISSION_DENIED;ACCESS_TOKEN_SCOPE_INSUFFICIENT",
        )


# ---------------------------------------------------------------------------
# Group sync: the error lands on the provider record and in the API answer
# ---------------------------------------------------------------------------


class _RaisingDirectory(OIDCGroupAdapter):
    """A directory whose listing makes a real call and lets the refusal propagate."""

    def __init__(self, provider: OIDCProvider, *, url: str) -> None:
        super().__init__(provider)
        self._url = url

    async def list_all_groups(self) -> list[GroupInfo]:
        async with httpx.AsyncClient() as client:
            response = await client.get(self._url)
            response.raise_for_status()
        return []

    async def test_connection(self) -> tuple[bool, str]:
        return True, "unused"


class TestSync:
    async def test_a_failed_listing_records_no_provider_text(
        self, tmp_path, audit_events: list[dict]
    ) -> None:
        provider = OIDCProvider(
            type="generic",
            issuer="https://sso.example.com",
            groups=GroupResolutionConfig(mode="api"),
        )
        registry = OIDCProviderRegistry(tmp_path / "oidc")
        registry.create("sso", provider)
        with refusing_api(403, {"errorCode": "E0000006", "errorSummary": SENTINEL}) as server:
            adapter = _RaisingDirectory(provider, url=f"{server.base_url}/users/{SENTINEL}/groups")
            result = await sync_provider(
                "sso", registry, GroupStore(tmp_path / "groups"), adapter=adapter
            )

        stored = registry.get("sso")
        assert stored is not None
        assert_no_sentinel(result["error"])
        assert_no_sentinel(logged(audit_events))
        assert stored.sync_error == result["error"]
        assert "HTTP 403" in result["error"]
        line = failure_line(audit_events, "list_all_groups")
        assert (line["status"], line["code"]) == (403, "E0000006")


# ---------------------------------------------------------------------------
# Group sync: a listing that fails is a failed sync, whichever page it fails on
# ---------------------------------------------------------------------------


def _sync_registry(tmp_path) -> OIDCProviderRegistry:
    """A registry holding the api-mode provider ``sso``, which the tests sync with an injected adapter."""
    registry = OIDCProviderRegistry(tmp_path / "oidc")
    registry.create(
        "sso",
        OIDCProvider(
            type="generic",
            issuer="https://sso.example.com",
            groups=GroupResolutionConfig(mode="api"),
        ),
    )
    return registry


def _assert_failed_sync(
    *, registry: OIDCProviderRegistry, result: dict, group_store: GroupStore, status: int
) -> None:
    """The sync reported ``status`` as an error and stored no group from the failed listing."""
    stored = registry.get("sso")
    assert stored is not None
    assert stored.last_sync_status == "error"
    assert stored.sync_error == result["error"]
    assert f"HTTP {status}" in result["error"]
    assert (result["created"], result["updated"], result["total"]) == (0, 0, 0)
    assert group_store.list() == []


class TestSyncIncompleteListing:
    async def test_a_graph_listing_that_fails_on_page_two_is_a_failed_sync(
        self, tmp_path, audit_events: list[dict]
    ) -> None:
        graph = LocalGraph()
        graph.pages[TENANT] = [
            [{"id": "g1", "displayName": "Engineers", "mail": "eng@example.com"}],
            [{"id": "g2", "displayName": "Analysts", "mail": "analysts@example.com"}],
        ]
        graph.fail_page[TENANT] = 1
        graph.start()
        registry = _sync_registry(tmp_path)
        group_store = GroupStore(tmp_path / "groups")
        try:
            result = await sync_provider(
                "sso", registry, group_store, adapter=_entra_adapter(graph.base_url)
            )
        finally:
            graph.stop()

        assert graph.principals == [TENANT, TENANT]
        _assert_failed_sync(registry=registry, result=result, group_store=group_store, status=400)
        line = failure_line(audit_events, "list_all_groups")
        assert (line["status"], line["code"]) == (400, "BadRequest")

    async def test_an_okta_listing_that_fails_on_page_two_is_a_failed_sync(
        self, tmp_path, audit_events: list[dict]
    ) -> None:
        okta = LocalOkta(
            pages=[
                [{"id": "00g1", "profile": {"name": "Engineers", "email": "eng@example.com"}}],
                [{"id": "00g2", "profile": {"name": "Analysts", "email": "analysts@example.com"}}],
            ]
        )
        okta.fail_page = 1
        okta.fail_status = 403
        okta.fail_body = {"errorCode": "E0000006", "errorSummary": "You do not have permission"}
        okta.start()
        registry = _sync_registry(tmp_path)
        group_store = GroupStore(tmp_path / "groups")
        try:
            result = await sync_provider(
                "sso", registry, group_store, adapter=_okta_adapter(okta.base_url)
            )
        finally:
            okta.stop()

        assert okta.pages_requested == [0, 1]
        _assert_failed_sync(registry=registry, result=result, group_store=group_store, status=403)
        line = failure_line(audit_events, "list_all_groups")
        assert (line["status"], line["code"]) == (403, "E0000006")

    async def test_a_refused_directory_listing_is_a_failed_sync_not_an_empty_directory(
        self, tmp_path, audit_events: list[dict]
    ) -> None:
        refusal = {"error": {"code": 403, "errors": [{"reason": "forbidden"}], "message": "no"}}
        registry = _sync_registry(tmp_path)
        group_store = GroupStore(tmp_path / "groups")
        with refusing_api(403, refusal) as server:
            result = await sync_provider(
                "sso", registry, group_store, adapter=_directory_adapter(server.base_url)
            )

        _assert_failed_sync(registry=registry, result=result, group_store=group_store, status=403)
        line = failure_line(audit_events, "list_all_groups")
        assert (line["status"], line["code"]) == (403, "forbidden")


def _assert_unread_listing(
    *, registry: OIDCProviderRegistry, result: dict, group_store: GroupStore, words: str
) -> None:
    """The sync failed with ``words`` in its error and stored no group from the listing."""
    stored = registry.get("sso")
    assert stored is not None
    assert stored.last_sync_status == "error"
    assert stored.sync_error == result["error"]
    assert words in result["error"]
    assert (result["created"], result["updated"], result["total"]) == (0, 0, 0)
    assert group_store.list() == []


def _numbered_okta_pages(count: int) -> list[list[dict[str, Any]]]:
    """``count`` Okta pages of one group each."""
    return [
        [{"id": f"00g{index}", "profile": {"name": f"group-{index}"}}] for index in range(count)
    ]


class TestSyncUnreadableListing:
    """A 200 page without the listing's shape, or a listing that never ends, is a failed sync."""

    async def test_an_okta_page_that_is_not_a_list_is_a_failed_sync(self, tmp_path) -> None:
        okta = LocalOkta(pages=_numbered_okta_pages(3))
        okta.malformed_page = 1
        okta.start()
        registry = _sync_registry(tmp_path)
        group_store = GroupStore(tmp_path / "groups")
        try:
            result = await sync_provider(
                "sso", registry, group_store, adapter=_okta_adapter(okta.base_url)
            )
        finally:
            okta.stop()

        assert okta.pages_requested == [0, 1]
        _assert_unread_listing(
            registry=registry, result=result, group_store=group_store, words="not a list of groups"
        )

    async def test_a_graph_page_with_no_value_list_is_a_failed_sync(self, tmp_path) -> None:
        graph = LocalGraph()
        graph.refuse[TENANT] = (200, {"error": {"code": "not a page"}})
        graph.start()
        registry = _sync_registry(tmp_path)
        group_store = GroupStore(tmp_path / "groups")
        try:
            result = await sync_provider(
                "sso", registry, group_store, adapter=_entra_adapter(graph.base_url)
            )
        finally:
            graph.stop()

        _assert_unread_listing(
            registry=registry, result=result, group_store=group_store, words="not a list of groups"
        )

    async def test_an_okta_listing_still_paging_at_the_limit_is_a_failed_sync(
        self, tmp_path
    ) -> None:
        okta = LocalOkta(pages=_numbered_okta_pages(6))
        okta.start()
        adapter = _okta_adapter(okta.base_url)
        adapter.MAX_LISTING_PAGES = 3
        registry = _sync_registry(tmp_path)
        group_store = GroupStore(tmp_path / "groups")
        try:
            result = await sync_provider("sso", registry, group_store, adapter=adapter)
        finally:
            okta.stop()

        assert okta.pages_requested == [0, 1, 2]
        _assert_unread_listing(
            registry=registry, result=result, group_store=group_store, words="after 3"
        )

    async def test_a_graph_listing_still_paging_at_the_limit_is_a_failed_sync(
        self, tmp_path
    ) -> None:
        graph = LocalGraph()
        graph.pages[TENANT] = [
            [{"id": f"g{index}", "displayName": f"g{index}"}] for index in range(6)
        ]
        graph.start()
        adapter = _entra_adapter(graph.base_url)
        adapter.MAX_LISTING_PAGES = 3
        registry = _sync_registry(tmp_path)
        group_store = GroupStore(tmp_path / "groups")
        try:
            result = await sync_provider("sso", registry, group_store, adapter=adapter)
        finally:
            graph.stop()

        assert graph.principals == [TENANT, TENANT, TENANT]
        _assert_unread_listing(
            registry=registry, result=result, group_store=group_store, words="after 3"
        )


# ---------------------------------------------------------------------------
# Group sync: a credential that is set but gives no access is a failed sync
# ---------------------------------------------------------------------------


class _NoTokenEntra(EntraAdapter):
    """Entra whose token request returns no token, as it does for a client secret Entra refuses.

    MSAL pins an https authority at login.microsoftonline.com, so the token endpoint
    cannot be a loopback server. ``_get_token`` is the seam where its answer arrives.
    """

    def _get_token(self) -> str | None:
        return None


def _assert_credential_failed_sync(
    *,
    audit_events: list[dict],
    group_store: GroupStore,
    registry: OIDCProviderRegistry,
    result: dict,
    error_type: str = "DirectoryCredentialError",
) -> None:
    """The sync recorded a failed run that blames the credential, and stored no group."""
    stored = registry.get("sso")
    assert stored is not None
    assert stored.last_sync_status == "error"
    assert stored.sync_error == result["error"]
    assert "could not use the configured credential" in result["error"]
    assert result["skipped"] is None
    assert (result["created"], result["updated"], result["total"]) == (0, 0, 0)
    assert group_store.list() == []
    assert failure_line(audit_events, "list_all_groups")["error_type"] == error_type


class TestSyncCredentialGivesNoAccess:
    async def test_an_entra_secret_that_gets_no_token_is_a_failed_sync(
        self, tmp_path, audit_events: list[dict], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DFE_TEST_ENTRA_SECRET", "a-secret-entra-refuses")
        provider = OIDCProvider(
            type="entra_id",
            issuer="https://login.microsoftonline.com/test-tenant/v2.0",
            client_id="app-id",
            groups=GroupResolutionConfig(
                mode="api", tenant_id="test-tenant", client_secret_env="DFE_TEST_ENTRA_SECRET"
            ),
        )
        registry = _sync_registry(tmp_path)
        group_store = GroupStore(tmp_path / "groups")

        result = await sync_provider("sso", registry, group_store, adapter=_NoTokenEntra(provider))

        _assert_credential_failed_sync(
            audit_events=audit_events, group_store=group_store, registry=registry, result=result
        )

    async def test_an_entra_that_cannot_be_reached_blames_the_network_not_the_secret(
        self, tmp_path, audit_events: list[dict], monkeypatch: pytest.MonkeyPatch, entra_unreachable
    ) -> None:
        monkeypatch.setenv("DFE_TEST_ENTRA_SECRET", "a-secret-entra-never-sees")
        provider = OIDCProvider(
            type="entra_id",
            issuer="https://login.microsoftonline.com/test-tenant/v2.0",
            client_id="app-id",
            groups=GroupResolutionConfig(
                mode="api", tenant_id="test-tenant", client_secret_env="DFE_TEST_ENTRA_SECRET"
            ),
        )
        registry = _sync_registry(tmp_path)
        group_store = GroupStore(tmp_path / "groups")

        result = await sync_provider("sso", registry, group_store, adapter=EntraAdapter(provider))

        stored = registry.get("sso")
        assert stored is not None
        assert stored.last_sync_status == "error"
        assert "could not be reached" in result["error"]
        assert "could not use the configured credential" not in result["error"]
        assert failure_line(audit_events, "list_all_groups")["transport_failure"] == (
            "connection_refused"
        )

    async def test_a_google_service_account_that_does_not_build_is_a_failed_sync(
        self, tmp_path, audit_events: list[dict], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DFE_TEST_IDP_SA_JSON", '{"type": "service_account"}')
        provider = OIDCProvider(
            type="google",
            issuer="https://accounts.google.com",
            groups=GroupResolutionConfig(
                mode="api", service_account_json_env="DFE_TEST_IDP_SA_JSON"
            ),
        )
        registry = _sync_registry(tmp_path)
        group_store = GroupStore(tmp_path / "groups")

        result = await sync_provider("sso", registry, group_store, adapter=GoogleAdapter(provider))

        _assert_credential_failed_sync(
            audit_events=audit_events, group_store=group_store, registry=registry, result=result
        )

    async def test_a_google_token_request_google_refuses_is_a_failed_sync(
        self,
        tmp_path,
        audit_events: list[dict],
        monkeypatch: pytest.MonkeyPatch,
        service_account_pem: str,
    ) -> None:
        registry = _sync_registry(tmp_path)
        group_store = GroupStore(tmp_path / "groups")
        with refusing_api(400, TOKEN_REFUSED) as token_endpoint:
            adapter = _token_refusing_adapter(
                monkeypatch=monkeypatch,
                pem=service_account_pem,
                token_uri=f"{token_endpoint.base_url}/token",
            )
            result = await sync_provider("sso", registry, group_store, adapter=adapter)

        assert token_endpoint.paths == ["/token"]
        _assert_credential_failed_sync(
            audit_events=audit_events,
            error_type="RefreshError",
            group_store=group_store,
            registry=registry,
            result=result,
        )
        assert failure_line(audit_events, "list_all_groups")["code"] == "invalid_grant"
        assert token_endpoint.base_url not in result["error"]
        assert_no_sentinel(result["error"])
        assert_no_sentinel(logged(audit_events))

    async def test_an_okta_token_the_directory_refuses_is_a_failed_sync(
        self, tmp_path, audit_events: list[dict]
    ) -> None:
        okta = LocalOkta(pages=[[]])
        okta.fail_page = 0
        okta.fail_status = 401
        okta.fail_body = {"errorCode": "E0000011", "errorSummary": "Invalid token provided"}
        okta.start()
        registry = _sync_registry(tmp_path)
        group_store = GroupStore(tmp_path / "groups")
        try:
            result = await sync_provider(
                "sso", registry, group_store, adapter=_okta_adapter(okta.base_url)
            )
        finally:
            okta.stop()

        assert okta.pages_requested == [0]
        _assert_failed_sync(registry=registry, result=result, group_store=group_store, status=401)
        line = failure_line(audit_events, "list_all_groups")
        assert (line["status"], line["code"]) == (401, "E0000011")


# ---------------------------------------------------------------------------
# The relying party's userinfo call
# ---------------------------------------------------------------------------


class TestUserinfo:
    async def test_a_refused_userinfo_call_logs_its_status_not_the_url(
        self, local_idp: LocalIdp, audit_events: list[dict]
    ) -> None:
        local_idp.userinfo_status = 401
        local_idp.userinfo_body = json.dumps(
            {"error": "invalid_token", "error_description": SENTINEL}
        )
        claims = {"sub": "u1"}

        filled = await fill_from_userinfo_endpoint(
            claims=claims,
            client=make_oauth_client(server_metadata_url=local_idp.metadata_url),
            issuer=local_idp.base_url,
            token={"access_token": "t", "token_type": "Bearer"},
        )

        assert filled == claims
        assert f"{local_idp.base_url}/userinfo" not in logged(audit_events)
        assert_no_sentinel(logged(audit_events))
        line = failure_line(audit_events, "userinfo")
        assert (line["status"], line["code"]) == (401, "invalid_token")
