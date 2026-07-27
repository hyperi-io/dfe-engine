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

    groups_overflowed: bool = False
    """True when the IdP signalled a group-claim overage (Entra >200 groups).

    When set, the ``groups`` list did NOT come from the token (the token carried
    only a ``_claim_names`` pointer); the RP fills it by calling the directory
    API. A consumer that only reads ``groups`` need not care - this is a
    diagnostic flag so the enrichment path is observable in logs and tests."""


def _has_group_overage(claims: dict[str, Any], claim_name: str) -> bool:
    """True when the IdP replaced the groups claim with an overage pointer.

    Entra (and other AAD-shaped IdPs) will not put a large group set in the
    token. Instead it emits ``_claim_names: {"groups": "src1"}`` alongside a
    ``_claim_sources`` entry pointing at a Graph endpoint. Detecting the
    ``_claim_names`` entry for the configured groups claim is enough to know the
    membership must be fetched out-of-band. Pure - reads the dict only.
    """
    names = claims.get("_claim_names")
    return isinstance(names, dict) and claim_name in names


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
    overflowed = _has_group_overage(userinfo_claims, claim_name)
    return NormalizedIdentity(
        subject=subject, email=email, groups=groups, groups_overflowed=overflowed
    )


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
        userinfo = dict(token.get("userinfo") or {})
        identity = extract_identity(provider, userinfo)
        # Enrich from the directory API in two cases: an Entra >200 overage (the
        # token dropped the groups array), or a provider that never puts groups
        # in the token at all (google-workspace, enrich_on_login).
        if identity.groups_overflowed or provider.groups.enrich_on_login:
            identity = await self._enrich_groups_from_directory(provider, identity, userinfo)
        return identity

    async def _enrich_groups_from_directory(
        self,
        provider: OIDCProvider,
        identity: NormalizedIdentity,
        userinfo: dict[str, Any],
    ) -> NormalizedIdentity:
        """Fetch the user's groups from the provider directory API.

        Runs ONLY after Authlib has validated the id_token, so it never weakens
        token validation. Handles two shapes with one path:
          - Entra >200 overage: the token carried a ``_claim_names`` pointer, not
            the groups; the fetched GUIDs resolve to roles by ``source_id`` just
            like a normal (<200) Entra login.
          - Providers with no groups claim (google-workspace): the directory is
            the only source of membership.

        The directory key is the provider's stable object id where present
        (Entra ``oid``), falling back to email then ``sub`` (Google's Directory
        API ``userKey`` accepts email or id, so either works).

        Fails safe: on overage the fetched set is authoritative even when empty
        (default deny); otherwise a populated token set is NOT stripped by a
        transient empty fetch.
        """
        from dfe_engine.auth.oidc.adapters import get_adapter

        directory_id = str(userinfo.get("oid") or identity.email or identity.subject)
        try:
            fetched = await get_adapter(provider).resolve_user_groups(directory_id)
        except Exception as exc:  # pragma: no cover - defensive; adapters fail open
            logger.warning(
                "OIDC RP: directory group enrichment failed",
                provider=provider.issuer,
                error=str(exc),
            )
            return identity
        group_ids = [g.id for g in fetched if g.id]
        logger.info(
            "OIDC RP: resolved groups via directory",
            provider=provider.issuer,
            overflow=identity.groups_overflowed,
            group_count=len(group_ids),
        )
        if identity.groups_overflowed or group_ids or not identity.groups:
            return identity.model_copy(update={"groups": group_ids})
        return identity
