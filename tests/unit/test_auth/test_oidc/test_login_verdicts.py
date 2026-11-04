#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_oidc/test_login_verdicts.py
#  Purpose:      The OIDC login verdict table, run through the relying party against a local IdP
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The OIDC login verdict table.

Each row is one browser login through the relying party and Authlib against a local
IdP, and the outcome the row expects.
"""

import base64
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from joserfc import jwt
from joserfc.jwk import RSAKey
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route

from dfe_engine.auth.oidc.models import OIDCProvider
from dfe_engine.auth.oidc.registry import OIDCProviderRegistry
from dfe_engine.auth.oidc.rp import build_relying_party
from tests.unit.test_auth.test_oidc.local_idp import LocalIdp

CLIENT_ID = "dfe-client"
REDIRECT_URI = "http://rp.test/callback"
ENTRA_OID = "6f1c-oid"
OVERAGE_GROUPS = ["g-1", "g-2"]

type Signer = Literal["idp", "stranger", "none", "unpublished", "rotated"]
type Flow = Literal[
    "normal", "no-state", "foreign-state", "other-session", "replayed-nonce", "replay"
]


@dataclass(frozen=True)
class Verdict:
    """One login attempt and the outcome it must have: ``allow``, else the class of the refusal."""

    id: str
    expected: str
    provider_type: Literal["generic", "google", "entra_id", "okta"] = "generic"
    claims: dict[str, Any] = field(default_factory=dict)
    signer: Signer = "idp"
    flow: Flow = "normal"
    enabled: bool = True
    discovery_issuer: bool = True
    groups: list[str] | None = None


VERDICTS = [
    Verdict("valid-generic", "allow", claims={"groups": ["soc"]}, groups=["soc"]),
    Verdict("valid-entra", "allow", "entra_id", {"groups": ["guid-1"]}, groups=["guid-1"]),
    Verdict("valid-okta", "allow", "okta", {"groups": ["Okta Admins"]}, groups=["Okta Admins"]),
    Verdict("valid-google", "allow", "google", groups=[]),
    Verdict(
        "entra-overage-resolved",
        "allow",
        "entra_id",
        {"_claim_names": {"groups": "src1"}, "oid": ENTRA_OID},
        groups=OVERAGE_GROUPS,
    ),
    Verdict("expired", "ExpiredTokenError", claims={"exp": -600}),
    Verdict("skew-exp-within-leeway", "allow", claims={"exp": -60}),
    Verdict("skew-exp-beyond-leeway", "ExpiredTokenError", claims={"exp": -300}),
    Verdict("skew-nbf-within-leeway", "allow", claims={"nbf": 60}),
    Verdict("skew-nbf-beyond-leeway", "InvalidClaimError", claims={"nbf": 600}),
    Verdict("wrong-aud", "InvalidClaimError", claims={"aud": "someone-else"}),
    Verdict(
        "wrong-aud-our-azp", "InvalidClaimError", claims={"aud": "someone-else", "azp": CLIENT_ID}
    ),
    Verdict("aud-list-with-ours", "allow", claims={"aud": [CLIENT_ID, "api"], "azp": CLIENT_ID}),
    Verdict("wrong-iss", "InvalidClaimError", claims={"iss": "https://evil.example"}),
    Verdict("discovery-without-issuer", "allow", discovery_issuer=False),
    Verdict(
        "discovery-without-issuer-wrong-iss",
        "InvalidClaimError",
        claims={"iss": "https://evil.example"},
        discovery_issuer=False,
    ),
    Verdict("missing-nonce", "MissingClaimError", claims={"nonce": None}),
    Verdict("at-hash-mismatch", "InvalidClaimError", claims={"at_hash": "AAAAAAAAAAAAAAAAAAAAAA"}),
    Verdict("bad-signature", "BadSignatureError", signer="stranger"),
    Verdict("alg-none", "UnsupportedAlgorithmError", signer="none"),
    Verdict("unknown-kid-rotated-in", "allow", signer="rotated"),
    Verdict("unknown-kid-never-published", "InvalidKeyIdError", signer="unpublished"),
    Verdict("replayed-nonce", "InvalidClaimError", flow="replayed-nonce"),
    Verdict("replayed-callback", "MismatchingStateError", flow="replay"),
    Verdict("missing-state", "MismatchingStateError", flow="no-state"),
    Verdict("mismatched-state", "MismatchingStateError", flow="foreign-state"),
    Verdict("pkce-verifier-mismatch", "OAuthError", flow="other-session"),
    Verdict("disabled-provider", "KeyError", enabled=False),
]


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _at_hash(access_token: str) -> str:
    digest = hashlib.sha256(access_token.encode("ascii")).digest()
    return _b64(digest[: len(digest) // 2])


def _id_token(*, idp: LocalIdp, claims: dict[str, Any], signer: Signer) -> str:
    """The ID token ``signer`` produces over ``claims``."""
    if signer == "none":
        header = _b64(json.dumps({"alg": "none", "typ": "JWT"}).encode())
        return f"{header}.{_b64(json.dumps(claims).encode())}."
    key = idp.signing_key
    if signer == "stranger":
        key = RSAKey.generate_key(2048, {"kid": "k1"})
    elif signer in ("unpublished", "rotated"):
        key = RSAKey.generate_key(2048, {"kid": f"k-{signer}"})
        if signer == "rotated":
            idp.published_keys = [idp.signing_key, key]
    return jwt.encode({"alg": "RS256", "kid": key.kid}, claims, key)


def _claims(*, idp: LocalIdp, nonce: str, access_token: str, row: Verdict) -> dict[str, Any]:
    """The row's ID token claims, its time offsets taken from now."""
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": idp.base_url,
        "sub": "user-1",
        "aud": CLIENT_ID,
        "iat": now,
        "exp": now + 300,
        "nonce": nonce,
        "at_hash": _at_hash(access_token),
        "name": "Ada",
        "email": "ada@example.com",
    }
    for name, value in row.claims.items():
        claims[name] = now + value if name in ("exp", "nbf") else value
    return {name: value for name, value in claims.items() if value is not None}


def _rp_app(*, idp: LocalIdp, row: Verdict, tmp_path: Any) -> Starlette:
    """An app serving the relying party's login and callback for one provider named ``idp``."""
    registry = OIDCProviderRegistry(tmp_path / "oidc-providers")
    provider = OIDCProvider(
        type=row.provider_type,
        enabled=row.enabled,
        issuer=idp.base_url,
        client_id=CLIENT_ID,
        client_secret_env="DFE_TEST_OIDC_SECRET",
        groups={"directory_backend": "mock"},
    )
    registry.create("idp", provider)
    rp = build_relying_party(registry)
    assert rp is not None

    async def login(request: Request) -> PlainTextResponse:
        try:
            url = await rp.login_authorization_url("idp", request, REDIRECT_URI)
        except KeyError:
            return PlainTextResponse("", status_code=404)
        return PlainTextResponse(url)

    async def callback(request: Request) -> JSONResponse:
        try:
            identity = await rp.handle_callback("idp", request)
        except Exception as exc:
            return JSONResponse({"denied": type(exc).__name__}, status_code=401)
        return JSONResponse({"groups": identity.groups, "subject": identity.subject})

    return Starlette(
        middleware=[Middleware(SessionMiddleware, secret_key="verdict-table")],
        routes=[Route("/login", login), Route("/callback", callback)],
    )


async def _begin(browser: httpx.AsyncClient) -> dict[str, str]:
    """Start a login and return the authorization request's parameters."""
    response = await browser.get("/login")
    if response.status_code != 200:
        return {}
    return {k: v[0] for k, v in parse_qs(urlsplit(response.text).query).items()}


async def _login(*, idp: LocalIdp, app: Starlette, row: Verdict) -> httpx.Response:
    """Run the row's login through ``app`` and return the callback's response."""
    transport = httpx.ASGITransport(app=app)
    async with (
        httpx.AsyncClient(transport=transport, base_url="http://rp.test") as browser,
        httpx.AsyncClient(transport=transport, base_url="http://rp.test") as other,
    ):
        if row.signer in ("rotated", "unpublished"):
            await _login(idp=idp, app=app, row=Verdict("warm-jwks", "allow"))
        request = await _begin(browser)
        if not request:
            return await browser.get("/callback", params={"code": "c", "state": "s"})
        nonce = request["nonce"]
        state = request["state"]
        challenge = request["code_challenge"]
        if row.flow == "replayed-nonce":
            nonce = (await _begin(other))["nonce"]
        if row.flow == "other-session":
            stolen = await _begin(other)
            challenge = stolen["code_challenge"]
        access_token = f"at-{row.id}"
        claims = _claims(idp=idp, nonce=nonce, access_token=access_token, row=row)
        id_token = _id_token(idp=idp, claims=claims, signer=row.signer)
        code = idp.issue_code(challenge=challenge, id_token=id_token, access_token=access_token)
        params = {"code": code, "state": state}
        if row.flow == "no-state":
            del params["state"]
        elif row.flow == "foreign-state":
            params["state"] = (await _begin(other))["state"]
        elif row.flow == "replay":
            await browser.get("/callback", params=params)
        return await browser.get("/callback", params=params)


@pytest.fixture
def overage_directory(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The Entra user's groups in a mock directory, and the login's client secret in the env."""
    fixture = tmp_path / "directory.json"
    groups = [{"id": gid, "name": gid} for gid in OVERAGE_GROUPS]
    fixture.write_text(json.dumps({"groups": groups, "members": {ENTRA_OID: OVERAGE_GROUPS}}))
    monkeypatch.setenv("DFE_ENGINE_OIDC_MOCK_DIRECTORY", str(fixture))
    monkeypatch.setenv("DFE_TEST_OIDC_SECRET", "s3cret")


@pytest.mark.parametrize("row", VERDICTS, ids=[row.id for row in VERDICTS])
async def test_login_verdict(
    row: Verdict, local_idp: LocalIdp, tmp_path: Any, overage_directory: None
) -> None:
    if not row.discovery_issuer:
        metadata = local_idp.metadata()
        del metadata["issuer"]
        local_idp.discovery_body = json.dumps(metadata)
    app = _rp_app(idp=local_idp, row=row, tmp_path=tmp_path)
    response = await _login(idp=local_idp, app=app, row=row)
    body = response.json()
    assert body.get("denied", "allow") == row.expected, body
    if row.groups is not None:
        assert body["groups"] == row.groups
    if row.signer in ("rotated", "unpublished"):
        assert local_idp.jwks_fetches == 2
