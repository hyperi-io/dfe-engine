#  Project:      dfe-engine
#  File:         auth/oidc/adapters/base.py
#  Purpose:      Abstract base class for OIDC group adapters, their directory listing error and page fetch
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Abstract base for OIDC group adapters.

Each provider type (generic, google, entra_id, okta) implements this ABC.
All methods are async to allow network calls without blocking the event loop.
"""

from __future__ import annotations

import traceback
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpx
from scalo.logger import logger

if TYPE_CHECKING:
    from scalo.http import AsyncHttpClient

    from dfe_engine.auth.oidc.models import GroupInfo, OIDCProvider
    from dfe_engine.secrets import DfeSecrets

# An error summary a directory sends is cut to this many characters before an operator sees it.
DIRECTORY_SUMMARY_LIMIT = 200


def _error_summary(*, read_summary: Callable[[Any], str], response: httpx.Response) -> str:
    """The directory's own one-line summary of a refused request; empty when its body carries none."""
    try:
        body = response.json()
    except ValueError:
        return ""
    return read_summary(body)[:DIRECTORY_SUMMARY_LIMIT]


class DirectoryError(Exception):
    """A directory listing failed; the message names the provider type and the failed request or missing setting, never a credential."""

    def __init__(self, *, detail: str, provider_type: str) -> None:
        """Prefix *detail* with the provider type, such as ``okta directory: ...``, keeping *detail* for a caller that names the provider itself."""
        super().__init__(f"{provider_type} directory: {detail}")
        self.detail = detail


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
        """Return every group in the provider's directory, for the group sync.

        Raises :class:`DirectoryError` when the directory cannot be listed in full, so a failed listing is never read as an empty directory.
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


def describe_error(*, exc: BaseException, read_summary: Callable[[Any], str] | None = None) -> str:
    """Describe *exc* for a log line or an API answer without its text, which can quote the request's credential.

    An HTTP error is its status, with the directory's own bounded summary when ``read_summary`` finds one in the body; anything else is its exception type.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        status = f"HTTP {exc.response.status_code}"
        summary = (
            _error_summary(read_summary=read_summary, response=exc.response)
            if read_summary is not None
            else ""
        )
        return f"{status}: {summary!r}" if summary else status
    return type(exc).__name__


def error_frames(*, exc: BaseException) -> str:
    """The file, line and source of each frame *exc* passed through, for a log line that must not carry the error's own text or local values."""
    return "".join(traceback.format_tb(exc.__traceback__))


async def fetch_directory_page(
    *,
    client: AsyncHttpClient,
    headers: dict[str, str],
    provider_type: str,
    read_summary: Callable[[Any], str],
    url: str,
) -> tuple[Any, httpx.Headers]:
    """GET one page of a directory listing and return its JSON body and response headers.

    Raises :class:`DirectoryError` naming the request path when the request cannot be built or sent (named by the error's type alone), the directory answers other than 2xx (with the summary ``read_summary`` finds in its body) or the body is not JSON.
    """
    path = urlsplit(url).path
    try:
        response = await client.get(url, headers=headers)
    except httpx.HTTPStatusError as exc:
        detail = f"GET {path!r} returned {describe_error(exc=exc, read_summary=read_summary)}"
        raise DirectoryError(detail=detail, provider_type=provider_type) from exc
    except Exception as exc:
        # Building or sending the request also fails outside httpx's own errors, such as a header value it cannot encode.
        error = describe_error(exc=exc)
        logger.warning(
            "Directory listing request failed",
            error=error,
            path=path,
            provider_type=provider_type,
        )
        raise DirectoryError(
            detail=f"GET {path!r} failed: {error}", provider_type=provider_type
        ) from exc
    try:
        body = response.json()
    except ValueError as exc:
        detail = f"GET {path!r} returned a body that is not JSON"
        raise DirectoryError(detail=detail, provider_type=provider_type) from exc
    return body, response.headers
