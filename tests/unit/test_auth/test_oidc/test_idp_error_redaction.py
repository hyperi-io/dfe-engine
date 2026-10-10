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
from authlib.integrations.base_client import OAuthError
from google.auth.credentials import AnonymousCredentials
from googleapiclient.discovery import build

from dfe_engine.auth.groups import GroupStore
from dfe_engine.auth.oidc.adapters.base import OIDCGroupAdapter
from dfe_engine.auth.oidc.adapters.entra import EntraAdapter
from dfe_engine.auth.oidc.adapters.google import GoogleAdapter
from dfe_engine.auth.oidc.adapters.okta import OktaAdapter
from dfe_engine.auth.oidc.idp_errors import describe_idp_error, provider_error_code
from dfe_engine.auth.oidc.models import GroupInfo, GroupResolutionConfig, OIDCProvider
from dfe_engine.auth.oidc.registry import OIDCProviderRegistry
from dfe_engine.auth.oidc.rp import fill_from_userinfo_endpoint
from dfe_engine.auth.oidc.sync import sync_provider
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
        assert describe_idp_error(ConnectionError(f"cannot reach {SENTINEL}")) == {
            "error_type": "ConnectionError"
        }

    @pytest.mark.parametrize(
        ("exc", "expected"),
        [
            (_caused_by(httpx.ConnectError("x"), socket.gaierror(-2, "unknown name")), "dns"),
            (_caused_by(httpx.ConnectError("x"), ssl.SSLCertVerificationError("bad")), "tls"),
            (_caused_by(httpx.ConnectError("x"), ConnectionRefusedError()), "connection_refused"),
            (_caused_by(httpx.ConnectError("x"), TimeoutError()), "timeout"),
            (httpx.ConnectTimeout("x"), "timeout"),
            (httpx.PoolTimeout("x"), "timeout"),
        ],
        ids=["dns", "tls", "refused", "socket-timeout", "connect-timeout", "pool-timeout"],
    )
    def test_a_failure_with_no_answer_says_why_by_the_socket_error_class(
        self, exc: BaseException, expected: str
    ) -> None:
        assert describe_idp_error(exc) == {
            "error_type": type(exc).__name__,
            "transport_failure": expected,
        }

    def test_a_cause_named_only_in_the_message_is_not_read(self) -> None:
        """The class decides: a message that mentions DNS, TLS or a timeout says nothing."""
        message = f"dns lookup of {SENTINEL} failed: tls handshake timed out, connection refused"

        assert describe_idp_error(ConnectionError(message)) == {"error_type": "ConnectionError"}

    def test_an_oauth_error_yields_its_code_and_not_its_description(self) -> None:
        """Authlib builds the error from the callback's query string, so the description is the caller's."""
        error = OAuthError(error="access_denied", description=f"{SENTINEL} may not sign in")

        assert describe_idp_error(error) == {"code": "access_denied", "error_type": "OAuthError"}


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
