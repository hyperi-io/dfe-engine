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
identity (sub/email/name/groups), and then RE-MINTS its own ES384 engine token via
``JwtAuthority``. Downstream apps only ever see the engine-issued token - the
external IdP's RS256 token never leaves the engine<->IdP leg.

Config never holds a secret: it holds the client_id in the clear, a PATH into
the DfeSecrets seam, and the NAME of an env var. The RP resolves the real
client_id/client_secret at registration time, store before env, so a secret
written through the provider API takes effect without a restart.

Claim extraction is a PURE function (``extract_identity``) kept separate from
the Authlib redirect plumbing so it is unit-testable without a live IdP.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from authlib.integrations.starlette_client import OAuth
from pydantic import BaseModel
from scalo.logger import logger

from dfe_engine.auth.oidc.credential_env import resolve_credential

if TYPE_CHECKING:
    from starlette.requests import Request

    from dfe_engine.auth.oidc.models import OIDCProvider
    from dfe_engine.auth.oidc.registry import OIDCProviderRegistry
    from dfe_engine.secrets import DfeSecrets

# Claims a userinfo response may fill; groups stay ID-token-only because they decide roles.
_USERINFO_FILL_CLAIMS = ("email", "name", "preferred_username")


class UserinfoSubjectMismatchError(ValueError):
    """The userinfo response names a different subject from the ID token."""


class NormalizedIdentity(BaseModel):
    """The identity extracted from an IdP id_token, before engine re-mint."""

    subject: str
    """The IdP ``sub`` claim - the stable user identifier."""

    email: str = ""
    """The user's email (``email`` claim), empty if the IdP did not assert one."""

    name: str = ""
    """The display name: the ``name`` claim, else ``preferred_username``, else empty."""

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


def _claim_text(*, claims: dict[str, Any], name: str) -> str:
    """The claim value with surrounding whitespace removed, or empty when absent or not a string."""
    value = claims.get(name)
    return value.strip() if isinstance(value, str) else ""


def _display_name(*, claims: dict[str, Any]) -> str:
    """The ``name`` claim, else ``preferred_username``, else empty."""
    return (_claim_text(claims=claims, name="name")) or (
        _claim_text(claims=claims, name="preferred_username")
    )


def extract_identity(
    provider: OIDCProvider,
    userinfo_claims: dict[str, Any],
) -> NormalizedIdentity:
    """Pure claim-extraction: map validated id_token claims to a NormalizedIdentity.

    ``sub`` is read from its standard OIDC claim. ``email`` falls back to
    ``preferred_username`` and then ``upn``: Entra emits no ``email`` for a
    cloud-only user with no mailbox, and the UPN is the identifier an operator
    recognises, so without the fallback such a user lands with a blank email.
    The display name is ``name``, falling back to ``preferred_username``. Groups
    come from the claim named by ``provider.groups.claim_name`` (default
    ``groups``), matching the token_claim resolution mode used across the DFE
    auth paths.

    No network calls, no Authlib state - safe to unit-test with a plain dict.

    Args:
        provider: The provider config (drives the groups claim name).
        userinfo_claims: The validated id_token / userinfo claims dict.

    Returns:
        A NormalizedIdentity with subject, email, and groups.
    """
    subject = str(userinfo_claims.get("sub") or "")
    email = ""
    for claim in ("email", "preferred_username", "upn"):
        email = str(userinfo_claims.get(claim) or "")
        if email:
            break
    name = _display_name(claims=userinfo_claims)
    claim_name = provider.groups.claim_name or "groups"
    groups = _coerce_groups(userinfo_claims.get(claim_name))
    overflowed = _has_group_overage(userinfo_claims, claim_name)
    return NormalizedIdentity(
        email=email, groups=groups, groups_overflowed=overflowed, name=name, subject=subject
    )


def merge_userinfo_claims(
    *, id_token_claims: dict[str, Any], userinfo_claims: dict[str, Any]
) -> dict[str, Any]:
    """Fill the profile claims the ID token left blank from the userinfo response.

    Only ``email``, ``name`` and ``preferred_username`` are filled, and only where
    the ID token has no non-blank value. A userinfo response for another subject
    raises UserinfoSubjectMismatchError, because OIDC Core 5.3.4 forbids using it.
    """
    if userinfo_claims.get("sub") != id_token_claims.get("sub"):
        raise UserinfoSubjectMismatchError("userinfo response subject does not match the ID token")
    merged = dict(id_token_claims)
    for claim in _USERINFO_FILL_CLAIMS:
        value = _claim_text(claims=userinfo_claims, name=claim)
        if value and not (_claim_text(claims=id_token_claims, name=claim)):
            merged[claim] = value
    return merged


async def fill_from_userinfo_endpoint(
    *, claims: dict[str, Any], client: Any, issuer: str, token: dict[str, Any]
) -> dict[str, Any]:
    """Fill a thin ID token's profile claims from the provider's userinfo endpoint.

    Okta's authorization-code flow is the common case: its ID token carries no
    profile or email claims. Any failure keeps the ID token claims unchanged, so
    the login still completes.
    """
    try:
        metadata = await client.load_server_metadata()
        if not (metadata.get("userinfo_endpoint")):
            return claims
        fetched = await client.userinfo(token=token)
        return merge_userinfo_claims(id_token_claims=claims, userinfo_claims=dict(fetched))
    except Exception as exc:
        logger.warning("OIDC RP: userinfo claims not used", error=str(exc), issuer=issuer)
        return claims


class OidcRelyingParty:
    """Authlib-backed OIDC relying party over the enabled providers.

    Builds one Authlib ``OAuth`` registry from the ENABLED providers in the
    OIDC provider registry. Each provider registers with client_id/client_secret
    resolved through ``resolve_credential`` (store before env) and an OIDC
    discovery URL, so Authlib validates the id_token against the IdP JWKS on
    callback.

    Zero enabled providers is a valid state: the RP holds an empty registry and
    every lookup reports the provider unknown (the router turns that into a 404).
    """

    def __init__(
        self, registry: OIDCProviderRegistry, *, secrets: DfeSecrets | None = None
    ) -> None:
        self._oauth = OAuth()
        self._secrets = secrets
        # Only providers we actually registered (enabled + non-empty issuer).
        self._providers: dict[str, OIDCProvider] = {}
        for name, provider in registry.list():
            if not provider.enabled:
                continue
            if not provider.issuer:
                logger.warning("OIDC RP: skipping provider with empty issuer", provider=name)
                continue
            client_id = resolve_credential(
                value=provider.client_id,
                env_name=provider.client_id_env,
                secrets=secrets,
            )
            client_secret = resolve_credential(
                secret_path=provider.client_secret_path,
                env_name=provider.client_secret_env,
                secrets=secrets,
            )
            self._oauth.register(
                name=name,
                # Authlib treats an unset credential as None, not as an empty string.
                client_id=client_id or None,
                client_secret=client_secret or None,
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

    async def login_authorization_url(
        self,
        provider_name: str,
        request: Request,
        redirect_uri: str,
    ) -> str:
        """Begin the auth-code flow and return the IdP authorize URL (SPA-friendly).

        Same session state as ``login_redirect`` (state + nonce in
        ``request.session``), but returns the URL for the browser to navigate
        explicitly—``fetch()`` cannot reliably follow a 302 to a cross-origin IdP.
        """
        client = self._client(provider_name)
        rv = await client.create_authorization_url(redirect_uri)
        await client.save_authorize_data(request, redirect_uri=redirect_uri, **rv)
        return str(rv["url"])

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
        ``extract_identity`` to get sub/email/name/groups.
        """
        provider = self._providers[provider_name]  # KeyError if unknown - caller guards
        client = self._client(provider_name)
        token = await client.authorize_access_token(request)
        # Authlib parses + validates the id_token and exposes its claims here.
        userinfo = dict(token.get("userinfo") or {})
        if not (_display_name(claims=userinfo)):
            userinfo = await fill_from_userinfo_endpoint(
                claims=userinfo, client=client, issuer=provider.issuer, token=token
            )
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
            adapter = get_adapter(provider, secrets=self._secrets)
            fetched = await adapter.resolve_user_groups(directory_id)
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


def build_relying_party(
    registry: OIDCProviderRegistry, *, secrets: DfeSecrets | None = None
) -> OidcRelyingParty | None:
    """Build an RP over the registry's CURRENT contents, or None if it cannot be built.

    An RP SNAPSHOTS the enabled providers at construction - both its own
    ``_providers`` map and the Authlib client registry underneath it, which
    caches one client per name and will NOT pick up a re-registration. So the RP
    has to be rebuilt wholesale whenever the registry changes; otherwise the
    login endpoints keep serving the provider set from process start and a
    freshly created provider 404s until a restart. Rebuilding is local work:
    Authlib fetches the discovery document lazily on first use, so nothing here
    touches the network.

    Never raises. RP setup is not allowed to break startup, and one provider
    with malformed config must not take a CRUD write down with it.
    """
    try:
        return OidcRelyingParty(registry, secrets=secrets)
    except Exception as exc:
        logger.warning("OIDC relying party unavailable", error=str(exc))
        return None
