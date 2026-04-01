#  Project:      dfe-engine
#  File:         auth/oidc/adapters/__init__.py
#  Purpose:      Factory for OIDC group adapters keyed by provider type
#  Language:     Python
#
#  License:      FSL-1.1-ALv2
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""OIDC group adapter factory.

Usage::

    from dfe_engine.auth.oidc.adapters import get_adapter

    adapter = get_adapter(provider)
    groups = await adapter.list_all_groups()
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from dfe_engine.auth.oidc.adapters.base import OIDCGroupAdapter
    from dfe_engine.auth.oidc.models import OIDCProvider


def get_adapter(provider: OIDCProvider) -> OIDCGroupAdapter:
    """Return the appropriate OIDCGroupAdapter for *provider*.

    Imports are lazy inside each branch so that optional provider-specific
    dependencies (e.g. Google API client) are only required when that
    provider type is actually used.

    Args:
        provider: The OIDCProvider configuration instance.

    Returns:
        An OIDCGroupAdapter implementation for the given provider type.

    Raises:
        ValueError: If the provider type is not supported.
    """
    match provider.type:
        case "generic":
            from dfe_engine.auth.oidc.adapters.generic import GenericAdapter

            return GenericAdapter(provider)
        case "google":
            from dfe_engine.auth.oidc.adapters.generic import GenericAdapter

            # Google adapter not yet implemented — fall back to generic
            # TODO: implement GoogleAdapter when google-auth extras are added
            return GenericAdapter(provider)
        case "entra_id":
            from dfe_engine.auth.oidc.adapters.generic import GenericAdapter

            # Entra ID adapter not yet implemented — fall back to generic
            # TODO: implement EntraIDAdapter when msal extras are added
            return GenericAdapter(provider)
        case "okta":
            from dfe_engine.auth.oidc.adapters.generic import GenericAdapter

            # Okta adapter not yet implemented — fall back to generic
            # TODO: implement OktaAdapter when okta extras are added
            return GenericAdapter(provider)
        case _:
            raise ValueError(f"Unsupported OIDC provider type: {provider.type!r}")
