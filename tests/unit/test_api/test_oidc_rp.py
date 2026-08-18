#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_oidc_rp.py
#  Purpose:      OIDC relying-party claim extraction + engine token re-mint (no mocks)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""OIDC RP unit tests.

Covers the pure claim-extraction + normalization and the engine token re-mint
against a REAL JwtAuthority over a real scalo.secrets file backend. The live
redirect flow (Authlib authorize_redirect / authorize_access_token against an
IdP) is out of scope here and validated against a real dex in integration - no
Authlib network calls are mocked.
"""

from __future__ import annotations

import jwt as pyjwt

from dfe_engine.auth.jwt_authority import JwtAuthority
from dfe_engine.auth.oidc.models import GroupResolutionConfig, OIDCProvider
from dfe_engine.auth.oidc.registry import OIDCProviderRegistry
from dfe_engine.auth.oidc.rp import NormalizedIdentity, extract_identity
from dfe_engine.secrets import build_secrets
from dfe_engine.settings import SecretsSettings

ISS = "https://dfe.test/api"


def _authority(path) -> JwtAuthority:
    secrets = build_secrets(SecretsSettings(provider="file", path=str(path)))
    return JwtAuthority(secrets, issuer=ISS)


# ── extract_identity (pure) ──────────────────────────────────────


def test_extract_identity_generic_default_claim():
    """Generic provider: sub/email/groups read from standard + default claim."""
    provider = OIDCProvider(type="generic", issuer="https://idp.example")
    claims = {
        "sub": "alice@example.com",
        "email": "alice@example.com",
        "groups": ["soc", "admins"],
        "aud": "dfe",
    }
    identity = extract_identity(provider, claims)
    assert identity.subject == "alice@example.com"
    assert identity.email == "alice@example.com"
    assert identity.groups == ["soc", "admins"]


def test_extract_identity_okta_default_claim():
    """Okta natively emits a 'groups' array claim - extracted the same way."""
    provider = OIDCProvider(type="okta", issuer="https://acme.okta.com")
    claims = {
        "sub": "00u1abc",
        "email": "bob@acme.com",
        "groups": ["Everyone", "dfe-analysts"],
    }
    identity = extract_identity(provider, claims)
    assert identity.subject == "00u1abc"
    assert identity.email == "bob@acme.com"
    assert identity.groups == ["Everyone", "dfe-analysts"]


def test_extract_identity_custom_claim_name():
    """A provider can point the groups resolution at a non-default claim name."""
    provider = OIDCProvider(
        type="entra_id",
        issuer="https://login.microsoftonline.com/tid/v2.0",
        groups=GroupResolutionConfig(mode="token_claim", claim_name="roles"),
    )
    claims = {"sub": "guid-123", "email": "carol@acme.com", "roles": ["group-guid-a"]}
    identity = extract_identity(provider, claims)
    assert identity.subject == "guid-123"
    assert identity.groups == ["group-guid-a"]


def test_extract_identity_string_groups_comma_split():
    """A comma-separated string groups claim is normalized to a list."""
    provider = OIDCProvider(type="generic", issuer="https://idp.example")
    claims = {"sub": "x", "email": "x@y.z", "groups": " soc , admins "}
    identity = extract_identity(provider, claims)
    assert identity.groups == ["soc", "admins"]


def test_extract_identity_missing_optional_fields():
    """Missing email/groups yield empty defaults, not errors."""
    provider = OIDCProvider(type="generic", issuer="https://idp.example")
    identity = extract_identity(provider, {"sub": "only-sub"})
    assert identity.subject == "only-sub"
    assert identity.email == ""
    assert identity.groups == []
    assert identity.groups_overflowed is False


# ── group-claim overage detection (Entra >200 groups) ───────────


def test_extract_identity_detects_group_overage():
    """An Entra >200-group overage marker sets the flag and leaves groups empty.

    When a user is in too many groups Entra omits the ``groups`` array and emits
    a ``_claim_names``/``_claim_sources`` pointer instead. extract_identity must
    notice that so the RP knows to fetch membership out-of-band, rather than
    silently treating the user as belonging to no groups.
    """
    provider = OIDCProvider(type="entra_id", issuer="https://login.microsoftonline.com/tid/v2.0")
    claims = {
        "sub": "pairwise-sub",
        "oid": "00000000-user-oid",
        "email": "big@acme.com",
        "_claim_names": {"groups": "src1"},
        "_claim_sources": {
            "src1": {
                "endpoint": "https://graph.microsoft.com/v1.0/users/00000000-user-oid/getMemberObjects"
            }
        },
    }
    identity = extract_identity(provider, claims)
    assert identity.groups == []
    assert identity.groups_overflowed is True


def test_extract_identity_no_overage_when_groups_present():
    """A normal groups array is not mistaken for an overage."""
    provider = OIDCProvider(type="entra_id", issuer="https://login.microsoftonline.com/tid/v2.0")
    claims = {"sub": "s", "email": "a@b.c", "groups": ["guid-a", "guid-b"]}
    identity = extract_identity(provider, claims)
    assert identity.groups == ["guid-a", "guid-b"]
    assert identity.groups_overflowed is False


# ── re-mint: NormalizedIdentity -> engine ES384 token ────────────


def test_remint_engine_token_is_es384_and_verifies(tmp_path):
    """A normalized identity re-mints an engine token that is ES384 and verifies."""
    authority = _authority(tmp_path)
    identity = NormalizedIdentity(
        subject="alice@example.com",
        email="alice@example.com",
        groups=["soc", "admins"],
    )

    token = authority.sign(
        {"sub": identity.subject, "email": identity.email, "groups": identity.groups}
    )

    # Header alg is ES384 (the engine is the single ES384 issuer).
    header = pyjwt.get_unverified_header(token)
    assert header["alg"] == "ES384"
    assert header["kid"] == authority.kid

    # Verifies against the same authority and carries the identity claims.
    claims = authority.verify(token)
    assert claims["sub"] == "alice@example.com"
    assert claims["email"] == "alice@example.com"
    assert claims["groups"] == ["soc", "admins"]
    assert claims["iss"] == ISS


def test_remint_empty_groups_roundtrips(tmp_path):
    """Re-mint with no groups still produces a valid engine token."""
    authority = _authority(tmp_path)
    identity = NormalizedIdentity(subject="svc-1", email="", groups=[])
    token = authority.sign(
        {"sub": identity.subject, "email": identity.email, "groups": identity.groups}
    )
    claims = authority.verify(token)
    assert claims["sub"] == "svc-1"
    assert claims["groups"] == []


# ── provider config round-trip (new RP-login fields) ─────────────


def test_provider_config_roundtrip_new_fields(tmp_path):
    """client_secret_env + scopes persist and reload via the YAML registry."""
    registry = OIDCProviderRegistry(tmp_path / "oidc-providers")
    provider = OIDCProvider(
        type="generic",
        enabled=True,
        issuer="https://idp.example",
        client_id_env="DFE_OIDC_CLIENT_ID",
        client_secret_env="DFE_OIDC_CLIENT_SECRET",
        scopes="openid email profile groups offline_access",
    )
    registry.create("acme", provider)

    reloaded = registry.get("acme")
    assert reloaded is not None
    assert reloaded.client_secret_env == "DFE_OIDC_CLIENT_SECRET"
    assert reloaded.scopes == "openid email profile groups offline_access"
    # Env var NAMES only - no secret material stored in config.
    assert reloaded.client_id_env == "DFE_OIDC_CLIENT_ID"


def test_provider_config_defaults():
    """New RP fields default to sensible values (no secret in config)."""
    provider = OIDCProvider(type="generic", issuer="https://idp.example")
    assert provider.client_secret_env == ""
    assert provider.scopes == "openid email profile groups"


# ── router wiring (real app, no live IdP) ────────────────────────


def test_login_unknown_provider_404(client):
    """The login route is mounted and the RP is attached: unknown provider -> 404.

    Proves the router wiring + app.state.oidc_rp without a live IdP (no provider
    is registered in the test app, so every provider name is unknown).
    """
    resp = client.get("/api/v1/auth/oidc/nonexistent/login", follow_redirects=False)
    assert resp.status_code == 404


def test_callback_unknown_provider_404(client):
    """The callback route is mounted and 404s for an unregistered provider."""
    resp = client.get("/api/v1/auth/oidc/nonexistent/callback", follow_redirects=False)
    assert resp.status_code == 404


class _FakeOidcRp:
    """Minimal RP stub for login route tests (no live IdP)."""

    def has_provider(self, name: str) -> bool:
        return name == "stub"

    async def login_redirect(self, provider: str, request, redirect_uri: str):
        from starlette.responses import RedirectResponse

        return RedirectResponse("https://idp.example/authorize", status_code=302)

    async def login_authorization_url(self, provider: str, request, redirect_uri: str) -> str:
        assert provider == "stub"
        assert redirect_uri.endswith("/api/v1/auth/oidc/stub/callback")
        return "https://idp.example/authorize?state=test"


def test_login_redirect_mode_302(client, app):
    app.state.oidc_rp = _FakeOidcRp()
    resp = client.get("/api/v1/auth/oidc/stub/login", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "https://idp.example/authorize"


def test_login_json_mode_returns_authorization_url(client, app):
    app.state.oidc_rp = _FakeOidcRp()
    resp = client.get("/api/v1/auth/oidc/stub/login?redirect=false")
    assert resp.status_code == 200
    assert resp.json() == {"authorization_url": "https://idp.example/authorize?state=test"}


# ── RP self-heal on an out-of-band registry change ───────────────
#
# The CRUD endpoints rebuild the RP on write, which only heals the worker that
# served it; the login path consults the registry before 404ing so a provider
# written by another worker or a gitops sync needs no engine restart.


class _Req:
    """Minimal stand-in for a Starlette Request - the reload path only reads app.state."""

    def __init__(self, app) -> None:
        self.app = app


def test_reload_picks_up_provider_written_out_of_band(client, app):
    """A provider written straight to the registry dir is usable without a restart."""
    from dfe_engine.api.v1.oidc_login import _reload_rp_if_provider_known

    assert app.state.oidc_rp is not None
    assert not app.state.oidc_rp.has_provider("gitops-added")

    app.state.oidc_provider_registry.create(
        "gitops-added", OIDCProvider(type="generic", issuer="https://idp.example")
    )

    rp = _reload_rp_if_provider_known(_Req(app), "gitops-added")
    assert rp is not None
    assert rp.has_provider("gitops-added")
    # Rebuilt in place, so the next request sees it too.
    assert app.state.oidc_rp is rp


def test_reload_ignores_unknown_provider(client, app):
    """A bogus name does NOT rebuild - an unauthenticated caller cannot force churn."""
    from dfe_engine.api.v1.oidc_login import _reload_rp_if_provider_known

    before = app.state.oidc_rp
    assert _reload_rp_if_provider_known(_Req(app), "no-such-provider") is None
    assert app.state.oidc_rp is before


def test_reload_ignores_disabled_provider(client, app):
    """A disabled provider stays unusable - the reload is not an enable back door."""
    from dfe_engine.api.v1.oidc_login import _reload_rp_if_provider_known

    app.state.oidc_provider_registry.create(
        "switched-off",
        OIDCProvider(type="generic", issuer="https://idp.example", enabled=False),
    )
    assert _reload_rp_if_provider_known(_Req(app), "switched-off") is None


def test_login_still_404s_for_a_provider_that_was_never_configured(client):
    """The reload path must not turn an unknown provider into anything but a 404."""
    resp = client.get("/api/v1/auth/oidc/never-configured/login", follow_redirects=False)
    assert resp.status_code == 404
