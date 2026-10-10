#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_oidc/test_google_delegation_token.py
#  Purpose:      The domain-wide-delegation token helper against a local token endpoint
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The helper behind the live Google test, run against a token endpoint on loopback.

The live test mints its user token with ``tests/support/google_delegation.py``.
Here a local endpoint receives the exchange the helper sends and the tests read
the claims off the wire, so a wrong ``sub``, ``scope``, ``aud`` or ``exp`` fails
without a Google tenant.
"""

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from tests.support.google_delegation import (
    ASSERTION_LIFETIME_SECONDS,
    GROUPS_READONLY_SCOPE,
    DelegationError,
    build_assertion,
    mint_user_access_token,
)
from tests.support.loopback import stop_server

USER = "alice@example.com"
CLIENT_EMAIL = "dfe-live@example-project.iam.gserviceaccount.com"
KEY_ID = "0123456789abcdef"
ACCESS_TOKEN = "synthetic-access-token"
DELEGATED_SCOPE = "https://www.googleapis.com/auth/cloud-identity.groups.readonly"


class LocalTokenEndpoint:
    """A token endpoint on 127.0.0.1 that records each exchange and answers one reply.

    ``status`` and ``body`` are the reply, and ``requests`` holds each form posted.
    """

    def __init__(self) -> None:
        self.status = 200
        self.body: bytes = json.dumps({"access_token": ACCESS_TOKEN}).encode("utf-8")
        self.requests: list[dict[str, Any]] = []
        endpoint = self

        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                """Record the form, then send the configured reply."""
                length = int(self.headers.get("Content-Length", "0"))
                form = parse_qs(self.rfile.read(length).decode("utf-8"))
                endpoint.requests.append(
                    {
                        "content_type": self.headers.get("Content-Type", ""),
                        "form": {key: values[0] for key, values in form.items()},
                    }
                )
                self.send_response(endpoint.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(endpoint.body)))
                self.end_headers()
                self.wfile.write(endpoint.body)

            def log_message(self, format: str, *args: object) -> None:
                """Keep request logging out of the test output."""

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(daemon=True, target=self._server.serve_forever)

    @property
    def token_uri(self) -> str:
        """The URL a service account key names as its ``token_uri``."""
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/token"

    def start(self) -> None:
        """Serve requests on a background thread."""
        self._thread.start()

    def stop(self) -> None:
        """Stop serving and release the socket."""
        stop_server(self._server, self._thread)


@pytest.fixture(scope="module")
def key_pair() -> tuple[str, str]:
    """A throwaway RSA key as (private PEM, public PEM)."""
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")
    public_pem = (
        private.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("ascii")
    )
    return private_pem, public_pem


@pytest.fixture
def endpoint():
    server = LocalTokenEndpoint()
    server.start()
    yield server
    server.stop()


def _service_account(token_uri: str, private_pem: str) -> dict[str, str]:
    """A service account key file in the shape Google issues, with synthetic values."""
    return {
        "client_email": CLIENT_EMAIL,
        "private_key": private_pem,
        "private_key_id": KEY_ID,
        "token_uri": token_uri,
        "type": "service_account",
    }


def _claims(assertion: str, *, public_pem: str, audience: str) -> dict[str, Any]:
    """The assertion's claims, after checking its signature, audience and expiry."""
    return jwt.decode(
        assertion,
        public_pem,
        algorithms=["RS256"],
        audience=audience,
        options={"require": ["aud", "exp", "iat", "iss", "scope", "sub"]},
    )


class TestExchange:
    async def test_posts_a_jwt_bearer_grant_and_returns_the_access_token(self, endpoint, key_pair):
        account = _service_account(endpoint.token_uri, key_pair[0])
        token = await mint_user_access_token(account, subject=USER)

        assert token == ACCESS_TOKEN
        assert len(endpoint.requests) == 1
        request = endpoint.requests[0]
        assert request["content_type"] == "application/x-www-form-urlencoded"
        assert set(request["form"]) == {"grant_type", "assertion"}
        assert request["form"]["grant_type"] == "urn:ietf:params:oauth:grant-type:jwt-bearer"

    async def test_the_assertion_names_the_user_the_scope_and_the_endpoint(
        self, endpoint, key_pair
    ):
        account = _service_account(endpoint.token_uri, key_pair[0])
        await mint_user_access_token(account, subject=USER)

        assertion = endpoint.requests[0]["form"]["assertion"]
        claims = _claims(assertion, public_pem=key_pair[1], audience=endpoint.token_uri)
        assert claims["sub"] == USER
        assert claims["scope"] == DELEGATED_SCOPE
        assert claims["aud"] == endpoint.token_uri
        assert claims["iss"] == CLIENT_EMAIL
        header = jwt.get_unverified_header(assertion)
        assert (header["alg"], header["kid"]) == ("RS256", KEY_ID)

    async def test_the_scope_defaults_to_groups_readonly(self, endpoint, key_pair):
        account = _service_account(endpoint.token_uri, key_pair[0])
        await mint_user_access_token(account, subject=USER)

        assertion = endpoint.requests[0]["form"]["assertion"]
        claims = _claims(assertion, public_pem=key_pair[1], audience=endpoint.token_uri)
        assert GROUPS_READONLY_SCOPE == DELEGATED_SCOPE
        assert claims["scope"] == DELEGATED_SCOPE

    async def test_a_scope_argument_replaces_the_default(self, endpoint, key_pair):
        account = _service_account(endpoint.token_uri, key_pair[0])
        await mint_user_access_token(account, subject=USER, scope="https://example.com/scope")

        assertion = endpoint.requests[0]["form"]["assertion"]
        claims = _claims(assertion, public_pem=key_pair[1], audience=endpoint.token_uri)
        assert claims["scope"] == "https://example.com/scope"


class TestAssertionLifetime:
    def test_expires_within_an_hour_of_now_and_of_its_own_issue_time(self, endpoint, key_pair):
        account = _service_account(endpoint.token_uri, key_pair[0])
        before = int(time.time())
        assertion = build_assertion(account, subject=USER, scope=GROUPS_READONLY_SCOPE)
        after = time.time()

        claims = _claims(assertion, public_pem=key_pair[1], audience=endpoint.token_uri)
        assert before <= claims["iat"] <= after
        assert claims["exp"] - claims["iat"] == ASSERTION_LIFETIME_SECONDS
        assert claims["exp"] - claims["iat"] <= 3600
        assert after < claims["exp"] <= after + 3600


class TestFailures:
    async def test_a_refusal_names_the_status_and_code_but_not_the_description(
        self, endpoint, key_pair
    ):
        endpoint.status = 400
        endpoint.body = json.dumps(
            {
                "error": "unauthorized_client",
                "error_description": f"Client is unauthorized to act as {USER}",
            }
        ).encode("utf-8")
        account = _service_account(endpoint.token_uri, key_pair[0])

        with pytest.raises(DelegationError) as raised:
            await mint_user_access_token(account, subject=USER)

        message = str(raised.value)
        assert "400" in message
        assert "unauthorized_client" in message
        assert USER not in message
        assert "unauthorized to act" not in message
        assert endpoint.requests[0]["form"]["assertion"] not in message

    async def test_a_refusal_with_no_json_body_has_no_code(self, endpoint, key_pair):
        endpoint.status = 502
        endpoint.body = b"<html>an intercepting proxy page</html>"
        account = _service_account(endpoint.token_uri, key_pair[0])

        with pytest.raises(DelegationError, match=r"HTTP 502, unspecified"):
            await mint_user_access_token(account, subject=USER)

    async def test_an_error_code_with_odd_characters_is_not_echoed(self, endpoint, key_pair):
        endpoint.status = 400
        endpoint.body = json.dumps({"error": f"bad {USER}"}).encode("utf-8")
        account = _service_account(endpoint.token_uri, key_pair[0])

        with pytest.raises(DelegationError) as raised:
            await mint_user_access_token(account, subject=USER)

        assert USER not in str(raised.value)
        assert "unspecified" in str(raised.value)

    @pytest.mark.parametrize(
        "body",
        [b"{}", b'{"access_token": ""}', b'{"access_token": 7}', b"not json", b"[]"],
    )
    async def test_an_answer_without_a_usable_access_token_is_an_error(
        self, endpoint, key_pair, body
    ):
        endpoint.body = body
        account = _service_account(endpoint.token_uri, key_pair[0])

        with pytest.raises(DelegationError, match="without an access token"):
            await mint_user_access_token(account, subject=USER)

    async def test_an_unreachable_endpoint_is_an_error(self, key_pair):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        account = _service_account(f"http://127.0.0.1:{port}/token", key_pair[0])

        with pytest.raises(DelegationError, match="unreachable"):
            await mint_user_access_token(account, subject=USER)

    @pytest.mark.parametrize(
        "field", ["client_email", "private_key", "private_key_id", "token_uri"]
    )
    async def test_a_key_file_missing_a_field_names_it_and_sends_nothing(
        self, endpoint, key_pair, field
    ):
        account = _service_account(endpoint.token_uri, key_pair[0])
        del account[field]

        with pytest.raises(DelegationError, match=field):
            await mint_user_access_token(account, subject=USER)

        assert endpoint.requests == []

    async def test_a_private_key_that_cannot_sign_is_an_error_that_does_not_echo_it(self, endpoint):
        not_a_key = "-----BEGIN PRIVATE KEY-----\nnot-a-real-key\n-----END PRIVATE KEY-----\n"
        account = _service_account(endpoint.token_uri, not_a_key)

        with pytest.raises(DelegationError, match="could not sign") as raised:
            await mint_user_access_token(account, subject=USER)

        assert "not-a-real-key" not in str(raised.value)
        assert endpoint.requests == []
