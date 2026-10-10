#  Project:      dfe-engine
#  File:         auth/oidc/adapters/__init__.py
#  Purpose:      Factory for OIDC group adapters keyed by provider type
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""OIDC group adapter factory.

Usage::

    from dfe_engine.auth.oidc.adapters import get_adapter

    adapter = get_adapter(provider, secrets=store)
    groups = await adapter.list_all_groups()
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from dfe_engine.auth.oidc.adapters.base import OIDCGroupAdapter
    from dfe_engine.auth.oidc.models import OIDCProvider
    from dfe_engine.secrets import DfeSecrets


def get_adapter(
    provider: OIDCProvider, *, access_token: str = "", secrets: DfeSecrets | None = None
) -> OIDCGroupAdapter:
    """Return the appropriate OIDCGroupAdapter for *provider*.

    Imports are lazy inside each branch so that optional provider-specific
    dependencies (e.g. Google API client) are only required when that
    provider type is actually used.

    Args:
        provider: The OIDCProvider configuration instance.
        access_token: The user's access token from the login being enriched.
            Only Google takes it: its directory answers the user about their own
            groups, where the others read with a credential of their own.
        secrets: The DfeSecrets seam the directory credential resolves through,
            ahead of the env var named in the provider config.

    Returns:
        An OIDCGroupAdapter implementation for the given provider type.

    Raises:
        ValueError: If the provider type is not supported.
    """
    # The Surface-B mock directory is a test/CI backend that stands in for ANY
    # provider type's directory API, so it is selected by config flag ahead of
    # the type match rather than being a provider type of its own.
    if provider.groups.directory_backend == "mock":
        from dfe_engine.auth.oidc.adapters.mock import MockDirectoryAdapter

        return MockDirectoryAdapter(provider, secrets=secrets)

    match provider.type:
        case "generic":
            from dfe_engine.auth.oidc.adapters.generic import GenericAdapter

            return GenericAdapter(provider, secrets=secrets)
        case "google":
            from dfe_engine.auth.oidc.adapters.google import GoogleAdapter

            return GoogleAdapter(provider, access_token=access_token, secrets=secrets)
        case "entra_id":
            from dfe_engine.auth.oidc.adapters.entra import EntraAdapter

            return EntraAdapter(provider, secrets=secrets)
        case "okta":
            from dfe_engine.auth.oidc.adapters.okta import OktaAdapter

            return OktaAdapter(provider, secrets=secrets)
        case _:
            raise ValueError(f"Unsupported OIDC provider type: {provider.type!r}")
