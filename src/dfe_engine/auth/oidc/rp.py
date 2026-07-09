#  Project:      dfe-engine
#  File:         auth/oidc/rp.py
#  Purpose:      OIDC Relying-Party - terminate the IdP login, normalize identity
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The DFE engine's OIDC Relying-Party (RP).

The engine is the RP and the SINGLE token issuer. An external IdP (Google,
Okta, Entra, or any OIDC-compliant provider) performs the interactive login;
the engine terminates the OIDC auth-code flow, validates the IdP's id_token
(signature/nonce/aud/exp against the IdP JWKS, done by Authlib), extracts the
identity (sub/email/groups), and then RE-MINTS its own ES384 engine token via
``JwtAuthority``. Downstream apps only ever see the engine-issued token - the
external IdP's RS256 token never leaves the engine<->IdP leg.

Credentials are stored in config as ENV VAR NAMES (never secrets in config);
the RP resolves the actual client_id/client_secret from the environment at
registration time.

Claim extraction is a PURE function (``extract_identity``) kept separate from
the Authlib redirect plumbing so it is unit-testable without a live IdP.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from authlib.integrations.starlette_client import OAuth
from pydantic import BaseModel
from scalo.logger import logger

if TYPE_CHECKING:
    from starlette.requests import Request

    from dfe_engine.auth.oidc.models import OIDCProvider
    from dfe_engine.auth.oidc.registry import OIDCProviderRegistry


class NormalizedIdentity(BaseModel):
    """The identity extracted from an IdP id_token, before engine re-mint."""

    subject: str
    """The IdP ``sub`` claim - the stable user identifier."""

    email: str = ""
    """The user's email (``email`` claim), empty if the IdP did not assert one."""

    groups: list[str] = []
    """Group identifiers pulled from the provider's configured groups claim."""


def _coerce_groups(raw: Any) -> list[str]:
    """Normalize a raw groups claim into a list of non-empty strings.

    IdPs deliver groups as a JSON array (the common case), or occasionally as a
    single comma/space separated string. Anything else yields an empty list.
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        # Split on comma first (Envoy-style), fall back to whitespace.
        parts = raw.split(",") if "," in raw else raw.split()
        return [p.strip() for p in parts if p.strip()]
    if isinstance(raw, (list, tuple)):
        return [str(g).strip() for g in raw if str(g).strip()]
    return []


def extract_identity(
    provider: OIDCProvider,
    userinfo_claims: dict[str, Any],
) -> NormalizedIdentity:
    """Pure claim-extraction: map validated id_token claims to a NormalizedIdentity.

    ``sub`` and ``email`` are read from their standard OIDC claims. Groups come
    from the claim named by ``provider.groups.claim_name`` (default ``groups``),
    matching the token_claim resolution mode used across the DFE auth paths.

    No network calls, no Authlib state - safe to unit-test with a plain dict.

    Args:
        provider: The provider config (drives the groups claim name).
        userinfo_claims: The validated id_token / userinfo claims dict.

    Returns:
        A NormalizedIdentity with subject, email, and groups.
    """
    subject = str(userinfo_claims.get("sub") or "")
    email = str(userinfo_claims.get("email") or "")
    claim_name = provider.groups.claim_name or "groups"
    groups = _coerce_groups(userinfo_claims.get(claim_name))
    return NormalizedIdentity(subject=subject, email=email, groups=groups)


class OidcRelyingParty:
    """Authlib-backed OIDC relying party over the enabled providers.

    Builds one Authlib ``OAuth`` registry from the ENABLED providers in the
    OIDC provider registry. Each provider registers with client_id/client_secret
    resolved from the env vars named in its config and an OIDC discovery URL, so
    Authlib validates the id_token against the IdP JWKS on callback.

    Zero enabled providers is a valid state: the RP holds an empty registry and
    every lookup reports the provider unknown (the router turns that into a 404).
    """

    def __init__(self, registry: OIDCProviderRegistry) -> None:
        self._oauth = OAuth()
        # Only providers we actually registered (enabled + non-empty issuer).
        self._providers: dict[str, OIDCProvider] = {}
        for name, provider in registry.list():
            if not provider.enabled:
                continue
            if not provider.issuer:
                logger.warning("OIDC RP: skipping provider with empty issuer", provider=name)
                continue
            client_id = os.environ.get(provider.client_id_env) if provider.client_id_env else None
            client_secret = (
                os.environ.get(provider.client_secret_env) if provider.client_secret_env else None
            )
            self._oauth.register(
                name=name,
                client_id=client_id,
                client_secret=client_secret,
                server_metadata_url=(
                    f"{provider.issuer.rstrip('/')}/.well-known/openid-configuration"
                ),
                client_kwargs={"scope": provider.scopes},
            )
            self._providers[name] = provider
            logger.info("OIDC RP registered provider", provider=name, issuer=provider.issuer)

    def has_provider(self, provider_name: str) -> bool:
        """True when ``provider_name`` is an enabled, registered provider."""
        return provider_name in self._providers

    def provider_names(self) -> list[str]:
        """Sorted names of the enabled, registered providers."""
        return sorted(self._providers)

    def _client(self, provider_name: str) -> Any:
        """Return the Authlib client for a provider or raise KeyError if unknown."""
        if provider_name not in self._providers:
            raise KeyError(f"OIDC provider '{provider_name}' is not registered or disabled")
        client = self._oauth.create_client(provider_name)
        if client is None:  # pragma: no cover - registry + _providers stay in lockstep
            raise KeyError(f"OIDC provider '{provider_name}' has no Authlib client")
        return client

    async def login_redirect(
        self,
        provider_name: str,
        request: Request,
        redirect_uri: str,
    ) -> Any:
        """Begin the auth-code flow: return Authlib's redirect to the IdP.

        Authlib stashes the OAuth state + nonce in ``request.session`` (hence the
        SessionMiddleware requirement) and returns a 302 to the IdP authorize
        endpoint.
        """
        client = self._client(provider_name)
        return await client.authorize_redirect(request, redirect_uri)

    async def handle_callback(self, provider_name: str, request: Request) -> NormalizedIdentity:
        """Complete the auth-code flow and return the normalized identity.

        ``authorize_access_token`` exchanges the code, then validates the
        id_token signature/nonce/aud/exp against the IdP JWKS. The parsed claims
        arrive under ``token['userinfo']``; we run them through the pure
        ``extract_identity`` to get sub/email/groups.
        """
        provider = self._providers[provider_name]  # KeyError if unknown - caller guards
        client = self._client(provider_name)
        token = await client.authorize_access_token(request)
        # Authlib parses + validates the id_token and exposes its claims here.
        userinfo = token.get("userinfo") or {}
        return extract_identity(provider, dict(userinfo))
