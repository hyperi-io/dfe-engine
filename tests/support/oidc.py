#  Project:      dfe-engine
#  File:         tests/support/oidc.py
#  Purpose:      Store the provider a stub relying party serves, so its logins are let through
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A login is served only for a provider the registry enables, stub relying party or not."""

from typing import Any

from dfe_engine.auth.oidc.models import OIDCProvider


def serve_provider(app: Any, name: str = "stub") -> None:
    """Store *name* enabled in the app's provider registry, as an RP built from it would have found it."""
    app.state.oidc_provider_registry.create(
        name, OIDCProvider(type="generic", issuer="https://idp.example")
    )
