#  Project:      dfe-engine
#  File:         auth/oidc/adapters/okta.py
#  Purpose:      Okta adapter stub for future implementation
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Okta OIDC group adapter — stub for future implementation.

Okta natively includes groups in the ID token via custom claim mapping,
so most deployments use ``token_claim`` mode and don't need this adapter.
The API-based group resolution will be implemented when a test environment
is available (free Okta developer account).
"""

from __future__ import annotations

from dfe_engine.auth.oidc.adapters.base import OIDCGroupAdapter
from dfe_engine.auth.oidc.models import GroupInfo


class OktaAdapter(OIDCGroupAdapter):
    """Okta Groups API adapter — stub, returns unfriendly fallback."""

    async def resolve_groups(self, group_ids: list[str]) -> dict[str, str]:
        """Return IDs as-is (unfriendly fallback until implemented)."""
        return {gid: gid for gid in group_ids}

    async def list_all_groups(self) -> list[GroupInfo]:
        """Not yet implemented — returns empty list."""
        return []

    async def test_connection(self) -> tuple[bool, str]:
        """Okta adapter not yet implemented."""
        return False, "Okta adapter not yet implemented — use token_claim mode"
