#  Project:      dfe-engine
#  File:         auth/oidc/adapters/base.py
#  Purpose:      Abstract base class for OIDC group resolution adapters
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Abstract base for OIDC group adapters.

Each provider type (generic, google, entra_id, okta) implements this ABC.
All methods are async to allow network calls without blocking the event loop.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from dfe_engine.auth.oidc.models import GroupInfo, OIDCProvider
    from dfe_engine.secrets import DfeSecrets


class OIDCGroupAdapter(ABC):
    """Abstract OIDC group adapter.

    Concrete implementations resolve group membership and enumerate groups
    from a specific identity provider.
    """

    def __init__(self, provider: OIDCProvider, *, secrets: DfeSecrets | None = None) -> None:
        self._provider = provider
        # The directory-API credential resolves through this seam before the env.
        self._secrets = secrets

    @abstractmethod
    async def resolve_groups(self, subject: str) -> list[GroupInfo]:
        """Return the groups that *subject* (OIDC sub claim) belongs to.

        Args:
            subject: The OIDC subject identifier for the authenticating user.

        Returns:
            List of GroupInfo records for the user's group memberships.
            Returns an empty list when the user belongs to no groups or when
            group resolution is not supported.
        """

    @abstractmethod
    async def list_all_groups(self) -> list[GroupInfo]:
        """Return all groups available in the provider.

        Used for bulk sync and admin enumeration.

        Returns:
            List of GroupInfo records.  Returns an empty list when the
            provider does not support group enumeration via API.
        """

    @abstractmethod
    async def test_connection(self) -> tuple[bool, str]:
        """Verify that the provider connection and credentials are working.

        Returns:
            A (success, message) tuple.  ``success`` is True when the
            connection succeeds, False otherwise.  ``message`` provides a
            human-readable status or error description.
        """

    async def resolve_user_groups(self, directory_id: str) -> list[GroupInfo]:
        """Return the groups *directory_id* belongs to, fetched from the provider.

        This is the enrichment call the Relying Party uses when a token does NOT
        carry the group membership itself - the classic case being Entra's >200
        group "overage", where the id_token replaces the ``groups`` array with a
        ``_claim_names`` pointer at a Graph endpoint. The RP then asks the adapter
        to enumerate the user's memberships directly.

        ``directory_id`` is the provider's stable object id for the user (Entra
        ``oid``, not the pairwise ``sub``), because that is what the directory API
        keys on.

        The default returns an empty list: providers without an admin directory
        API (generic) or that always deliver groups in-token have nothing to add,
        and returning ``[]`` fails safe (no groups -> default deny, never an
        accidental grant). Adapters that CAN enumerate a user's groups override
        this.
        """
        return []
