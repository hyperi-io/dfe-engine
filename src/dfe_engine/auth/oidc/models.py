#  Project:      dfe-engine
#  File:         auth/oidc/models.py
#  Purpose:      Pydantic models for OIDC provider configuration and group resolution
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Pydantic models for OIDC provider registry.

Providers are stored as YAML files -- one per provider.  The provider name
is the filename stem and is NOT stored inside the YAML body.

Supported provider types:
    generic   -- Any OIDC-compliant IdP; groups resolved from token claims only.
    google    -- Google Workspace; each login's groups read from Cloud Identity with the user's token.
    entra_id  -- Microsoft Entra ID (Azure AD); supports Graph API group sync.
    okta      -- Okta; supports API-based group resolution.

Group resolution modes:
    manual       -- Group membership managed manually in dfe-engine; the token's groups are ignored and nothing syncs.
    token_claim  -- Groups extracted from a named OIDC token claim at login time (the default).
    api          -- Groups fetched from the provider's admin API on a schedule.
"""

from typing import Literal, Self

from pydantic import BaseModel, Field, model_validator

# ---------------------------------------------------------------------------
# Group resolution configuration
# ---------------------------------------------------------------------------

_GroupResolutionMode = Literal["manual", "token_claim", "api"]
_ProviderType = Literal["generic", "google", "entra_id", "okta"]

# Google and Entra fail a login that asks for `groups`; Google's groups are read with the login's own Cloud Identity scope, and dex and Okta emit the groups claim only when it is requested.
_DEFAULT_SCOPES: dict[_ProviderType, str] = {
    "entra_id": "openid email profile",
    "generic": "openid email profile groups",
    "google": "openid email profile https://www.googleapis.com/auth/cloud-identity.groups.readonly",
    "okta": "openid email profile groups",
}

# The scopes field's default before it followed the type, sorted; every provider saved then stores it.
_LEGACY_DEFAULT_SCOPES = ("email", "groups", "openid", "profile")


class GroupResolutionConfig(BaseModel):
    """Configuration for how group membership is resolved from a provider.

    Fields that are provider-specific are only relevant when the matching
    provider type is active.  They are stored in the same model for
    simplicity -- unused fields default to empty strings.
    """

    mode: _GroupResolutionMode = "token_claim"
    """How groups are resolved: manual, token_claim, or api."""

    claim_name: str = "groups"
    """Token claim name containing group identifiers (used by token_claim mode)."""

    sync_interval: int = 3600
    """Seconds between API-based group sync cycles (used by api mode)."""

    enrich_on_login: bool = False
    """Fetch the user's groups from the directory API at each login.

    Needed by providers that do NOT put group membership in the token at all -
    Google Workspace is the case: its id_token carries no groups claim, so the
    only way to know a user's groups is to ask its directory for them at login,
    with the user's own token. When set (with ``mode == "api"``) the RP calls the adapter's
    ``resolve_user_groups`` and the directory result is authoritative. Entra's
    >200 overage is handled automatically without this flag (the token's overage
    marker triggers the same enrichment); a provider that reliably delivers
    groups in-token (dex, okta) leaves this off."""

    # -- Directory backend selection (test / CI seam) --
    directory_backend: Literal["live", "mock"] = "live"
    """Which directory backend api-mode enrichment uses.

    ``live`` hits the real provider API (Graph / Admin SDK / Okta). ``mock``
    selects the in-process Surface-B directory: a deterministic, offline group
    directory read from a JSON fixture, so api-mode and the Entra >200 overage
    path can be exercised in CI without a live tenant. This is a TEST/CI seam and
    is deliberately NOT settable through the provider admin API - flip it in the
    provider YAML only. Never ``mock`` in a real deployment."""

    mock_directory_env: str = "DFE_OIDC_MOCK_DIRECTORY"
    """Env var name whose value is the path to the Surface-B directory JSON fixture.

    Read only when ``directory_backend == 'mock'``. A path is not a secret, but it
    is still named indirectly via an env var to match the rest of this config and
    to keep absolute test paths out of committed YAML."""

    # -- Google Workspace (api mode): an optional service account; logins need none --
    service_account_json_env: str = ""
    """Env var name holding the Google service account JSON credentials."""

    service_account_json_path: str = ""
    """DfeSecrets path holding the Google service account JSON - a path, never the JSON."""

    domain: str = ""
    """Google Workspace primary domain (e.g. 'example.com') the service account syncs and probes."""

    # -- Microsoft Entra ID (api mode and the >200 group overage lookup) --
    tenant_id: str = ""
    """The Entra ID tenant ID itself - it is not secret, so it is held in the clear; ``tenant_id_env`` still resolves it when this is empty."""

    tenant_id_env: str = ""
    """Env var name holding the Entra ID tenant ID."""

    client_secret_env: str = ""
    """Env var name holding the Entra ID application client secret; the login client secret is used when neither this nor ``client_secret_path`` is set."""

    client_secret_path: str = ""
    """DfeSecrets path holding the Entra ID application client secret."""

    # -- Okta (api mode) --
    api_token_env: str = ""
    """Env var name holding the Okta API token."""

    api_token_path: str = ""
    """DfeSecrets path holding the Okta API token."""

    okta_domain: str = ""
    """Okta organisation domain (e.g. 'example.okta.com')."""


# ---------------------------------------------------------------------------
# OIDC provider
# ---------------------------------------------------------------------------


class OIDCProvider(BaseModel):
    """An OIDC identity provider configuration.

    The provider name is the filename stem of the YAML file -- it is NOT
    stored as a field here.  Secret material is stored as a PATH into the
    DfeSecrets seam or as the NAME of an env var, never as the value itself.
    """

    type: _ProviderType = "generic"
    """Provider implementation type."""

    enabled: bool = True
    """Whether this provider is active."""

    display_name: str = ""
    """Human-readable label shown in UI provider selection."""

    issuer: str = ""
    """OIDC issuer URL (e.g. 'https://accounts.google.com')."""

    client_id: str = ""
    """The OIDC client ID itself - it is not secret, so it is held in the clear.

    ``client_id_env`` still resolves it when this is empty, so a deployment that
    injects the id through the environment keeps working."""

    client_id_env: str = ""
    """Env var name holding the OIDC client ID."""

    client_secret_env: str = ""
    """Env var name holding the OIDC client secret used in the RP auth-code exchange.

    Distinct from ``GroupResolutionConfig.client_secret_env`` (that one backs the
    Entra Graph API sync). This one is the confidential-client secret the engine
    presents to the IdP token endpoint when terminating the login flow.
    """

    client_secret_path: str = ""
    """DfeSecrets path holding the RP client secret - a path, never the secret."""

    scopes: str = ""
    """Space-separated OAuth scopes requested at login. ``openid`` is mandatory.

    Left empty, it is filled with the provider type's default on load: ``openid
    email profile``, plus ``groups`` for ``generic`` and ``okta``, and the Cloud
    Identity groups read scope for ``google``."""

    groups: GroupResolutionConfig = Field(default_factory=GroupResolutionConfig)
    """Group resolution configuration for this provider."""

    created_at: str = ""
    """ISO 8601 UTC timestamp of initial creation."""

    last_sync_at: str = ""
    """ISO 8601 UTC timestamp of most recent group sync (api mode only)."""

    last_sync_status: str = ""
    """Human-readable result of the most recent sync ('ok', 'error', etc.)."""

    sync_error: str = ""
    """Error message from the most recent failed sync, if any."""

    @model_validator(mode="after")
    def _fill_default_scopes(self) -> Self:
        """Give a provider with no scopes configured the ones its type needs.

        A google provider still holding the old field default never had its
        scopes set by an operator, and Google refuses that set, so it counts as
        unset too.
        """
        stored = self.scopes.split()
        legacy_google = self.type == "google" and tuple(sorted(stored)) == _LEGACY_DEFAULT_SCOPES
        if not stored or legacy_google:
            self.scopes = _DEFAULT_SCOPES[self.type]
        return self


# ---------------------------------------------------------------------------
# Group information returned from provider APIs
# ---------------------------------------------------------------------------


class GroupInfo(BaseModel):
    """A group record fetched from a provider's API.

    Used as the return type of ``OIDCGroupAdapter.list_all_groups`` and
    ``OIDCGroupAdapter.resolve_user_groups``.  The ``id`` is provider-specific
    (e.g. Google group id, Entra ID object ID, Okta group ID).
    """

    id: str
    """Provider-specific group identifier."""

    name: str
    """Display name of the group."""

    email: str = ""
    """Group email address (where supported by the provider)."""

    description: str = ""
    """Group description (where supported by the provider)."""
