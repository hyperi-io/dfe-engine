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

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from dfe_engine.auth.oidc.models import GroupInfo, OIDCProvider
    from dfe_engine.secrets import DfeSecrets


class DirectoryNotConfiguredError(Exception):
    """A provider's directory cannot be listed because no credential for it is configured.

    An adapter raises this from ``list_all_groups`` instead of returning an empty list,
    so group sync can tell a directory it never read from one that has no groups.
    """


class DirectoryCredentialError(Exception):
    """A provider's directory credential is configured but gives no access to its directory.

    An adapter raises this from ``list_all_groups`` when Entra issues no token for the
    configured client secret, or the configured Google service account JSON does not
    build a client. Group sync records the run as failed, never as a directory with no groups.
    """


class DirectoryCredentialUnreadableError(DirectoryCredentialError):
    """A provider's directory credential is configured, but the secret store holding it did not answer.

    The credential may be sound, so group sync records a failed run that names the
    secret store rather than the credential.
    """


class NoDirectoryListingError(Exception):
    """The provider has no directory listing to sync: each login reads that user's groups with their own token.

    A Google provider with no service account works this way by default, so group
    sync records it as its own state, neither a failure nor a missing credential.
    """


class DirectoryPageError(Exception):
    """A directory answered a listing page without the listing's shape.

    The page holds no groups the adapter can read, so the listing cannot be trusted
    to be the whole directory.
    """


class DirectoryPageLimitError(Exception):
    """A directory listing ran past the page limit without reaching its last page.

    Attributes:
        pages: The page limit the listing reached.
    """

    def __init__(self, pages: int) -> None:
        """Record the page limit the listing reached."""
        super().__init__(pages)
        self.pages = pages


class OIDCGroupAdapter(ABC):
    """Abstract OIDC group adapter.

    Concrete implementations resolve group membership and enumerate groups
    from a specific identity provider.
    """

    # Pages a directory listing reads at most, so a provider that keeps linking a next page cannot hold the sync: 200,000 Okta or Google groups, 999,000 Graph groups.
    MAX_LISTING_PAGES = 1000

    def __init__(self, provider: OIDCProvider, *, secrets: DfeSecrets | None = None) -> None:
        self._provider = provider
        # The directory-API credential resolves through this seam before the env.
        self._secrets = secrets

    @abstractmethod
    async def list_all_groups(self) -> list[GroupInfo]:
        """Return all groups available in the provider.

        Used for bulk sync and admin enumeration.

        Returns:
            List of GroupInfo records.  Returns an empty list when the
            provider does not support group enumeration via API.

        Raises:
            NoDirectoryListingError: The provider reads each login's groups with
                the user's own token and has no directory to list.
            DirectoryNotConfiguredError: The provider has no credential to
                enumerate with.  Group sync reports that as its own state, never
                as a directory with no groups.
            DirectoryCredentialUnreadableError: The credential is configured but
                the secret store holding it did not answer.  A failed run.
            DirectoryCredentialError: The credential is configured but gives no
                access to the directory.  Group sync reports that as a failed run.
            DirectoryPageError: A page arrived without the listing's shape.
            DirectoryPageLimitError: The listing ran past :attr:`MAX_LISTING_PAGES`.
            Exception: Whatever the provider's API produced when a listing failed,
                on any page. An empty or short list never means a failed one,
                because group sync records what it is given as the whole directory.
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
