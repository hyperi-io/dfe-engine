#  Project:      dfe-engine
#  File:         auth/oidc/models.py
#  Purpose:      Pydantic models for OIDC provider configuration and group resolution
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Pydantic models for OIDC provider registry.

Providers are stored as YAML files — one per provider.  The provider name
is the filename stem and is NOT stored inside the YAML body.

Supported provider types:
    generic   — Any OIDC-compliant IdP; groups resolved from token claims only.
    google    — Google Workspace; supports admin SDK group enumeration.
    entra_id  — Microsoft Entra ID (Azure AD); supports Graph API group sync.
    okta      — Okta; supports API-based group resolution.

Group resolution modes:
    manual       — Group membership managed manually in dfe-engine; no sync.
    token_claim  — Groups extracted from a named OIDC token claim at login time.
    api          — Groups fetched from the provider's admin API on a schedule.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Group resolution configuration
# ---------------------------------------------------------------------------

_GroupResolutionMode = Literal["manual", "token_claim", "api"]
_ProviderType = Literal["generic", "google", "entra_id", "okta"]


class GroupResolutionConfig(BaseModel):
    """Configuration for how group membership is resolved from a provider.

    Fields that are provider-specific are only relevant when the matching
    provider type is active.  They are stored in the same model for
    simplicity — unused fields default to empty strings.
    """

    mode: _GroupResolutionMode = "manual"
    """How groups are resolved: manual, token_claim, or api."""

    claim_name: str = "groups"
    """Token claim name containing group identifiers (used by token_claim mode)."""

    sync_interval: int = 3600
    """Seconds between API-based group sync cycles (used by api mode)."""

    # -- Google Workspace (api mode) --
    service_account_json_env: str = ""
    """Env var name holding the Google service account JSON credentials."""

    admin_email: str = ""
    """Google Workspace admin email used for domain-wide delegation."""

    domain: str = ""
    """Google Workspace primary domain (e.g. 'example.com')."""

    # -- Microsoft Entra ID (api mode) --
    tenant_id_env: str = ""
    """Env var name holding the Entra ID tenant ID."""

    client_secret_env: str = ""
    """Env var name holding the Entra ID application client secret."""

    # -- Okta (api mode) --
    api_token_env: str = ""
    """Env var name holding the Okta API token."""

    okta_domain: str = ""
    """Okta organisation domain (e.g. 'example.okta.com')."""


# ---------------------------------------------------------------------------
# OIDC provider
# ---------------------------------------------------------------------------


class OIDCProvider(BaseModel):
    """An OIDC identity provider configuration.

    The provider name is the filename stem of the YAML file — it is NOT
    stored as a field here.  All credential material is stored as env var
    names (strings), never as secrets directly.
    """

    type: _ProviderType = "generic"
    """Provider implementation type."""

    enabled: bool = True
    """Whether this provider is active."""

    display_name: str = ""
    """Human-readable label shown in UI provider selection."""

    issuer: str = ""
    """OIDC issuer URL (e.g. 'https://accounts.google.com')."""

    client_id_env: str = ""
    """Env var name holding the OIDC client ID."""

    client_secret_env: str = ""
    """Env var name holding the OIDC client secret used in the RP auth-code exchange.

    Distinct from ``GroupResolutionConfig.client_secret_env`` (that one backs the
    Entra Graph API sync). This one is the confidential-client secret the engine
    presents to the IdP token endpoint when terminating the login flow.
    """

    scopes: str = "openid email profile groups"
    """Space-separated OAuth scopes requested at login. ``openid`` is mandatory."""

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


# ---------------------------------------------------------------------------
# Group information returned from provider APIs
# ---------------------------------------------------------------------------


class GroupInfo(BaseModel):
    """A group record fetched from a provider's API.

    Used as the return type of ``OIDCGroupAdapter.list_all_groups`` and
    ``OIDCGroupAdapter.resolve_groups``.  The ``id`` is provider-specific
    (e.g. Google group key, Entra ID object ID, Okta group ID).
    """

    id: str
    """Provider-specific group identifier."""

    name: str
    """Display name of the group."""

    email: str = ""
    """Group email address (where supported by the provider)."""

    description: str = ""
    """Group description (where supported by the provider)."""
