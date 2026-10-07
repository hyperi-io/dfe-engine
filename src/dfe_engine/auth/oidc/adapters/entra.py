#  Project:      dfe-engine
#  File:         auth/oidc/adapters/entra.py
#  Purpose:      Microsoft Entra ID adapter for Graph API group resolution
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Microsoft Entra ID (Azure AD) OIDC group adapter.

Resolves group GUIDs to display names via the Microsoft Graph API.
Requires an Entra app registration with the ``Group.Read.All`` application
permission and admin consent granted.

Credentials resolve through :func:`graph_credentials`. If any credential is missing the login-path methods fail open, returning unfriendly fallbacks rather than raising; ``list_all_groups`` raises :class:`DirectoryError` so a group sync reports the failure.

Usage::

    from dfe_engine.auth.oidc.adapters.entra import EntraAdapter

    adapter = EntraAdapter(provider, secrets=store)
    groups = await adapter.list_all_groups()
    display_names = await adapter.resolve_groups(group_ids)
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import msal
from pydantic import ValidationError
from scalo.logger import logger

from dfe_engine.auth.oidc.adapters.base import (
    DIRECTORY_SUMMARY_LIMIT,
    DirectoryError,
    OIDCGroupAdapter,
    describe_error,
    error_frames,
    fetch_directory_page,
)
from dfe_engine.auth.oidc.credential_env import resolve_credential
from dfe_engine.auth.oidc.models import GroupInfo

if TYPE_CHECKING:
    from dfe_engine.auth.oidc.models import OIDCProvider
    from dfe_engine.secrets import DfeSecrets


def _graph_error_summary(body: Any) -> str:
    """The ``error.message`` Graph puts in an error response; empty when there is none."""
    error = body.get("error") if isinstance(body, dict) else None
    message = error.get("message") if isinstance(error, dict) else None
    return message if isinstance(message, str) else ""


def _graph_page(*, body: Any, provider_type: str, url: str) -> tuple[list[GroupInfo], str]:
    """The groups on one page of a Graph listing and the next page's URL, empty on the last page.

    Raises :class:`DirectoryError` when the body is not an object holding a ``value`` list of group objects.
    """
    detail = f"GET {urlsplit(url).path!r} returned a body that is not a list of groups"
    items = body.get("value") if isinstance(body, dict) else None
    next_url = body.get("@odata.nextLink", "") if isinstance(body, dict) else ""
    if not (isinstance(items, list)) or not (isinstance(next_url, str)):
        raise DirectoryError(detail=detail, provider_type=provider_type)
    try:
        groups = [
            GroupInfo(
                description=item.get("description", "") or "",
                email=item.get("mail", "") or "",
                id=item.get("id", ""),
                name=item.get("displayName", ""),
            )
            for item in items
        ]
    except (AttributeError, ValidationError) as exc:
        # A group that is not an object has no .get; a non-string id or name fails validation.
        raise DirectoryError(detail=detail, provider_type=provider_type) from exc
    return groups, next_url


def _missing_graph_settings(*, credentials: GraphCredentials) -> list[str]:
    """The settings a Graph token needs that resolved empty, quoted as the provider API names them."""
    settings = {
        "groups.tenant_id": credentials.tenant_id,
        "client_id": credentials.client_id,
        "groups.client_secret": credentials.client_secret,
    }
    return [repr(name) for name, value in settings.items() if not (value)]


@dataclass(frozen=True, slots=True)
class GraphCredentials:
    """The app-only credentials a Graph API token is requested with; an empty field was not configured."""

    client_id: str
    client_secret: str
    tenant_id: str


def graph_credentials(*, provider: OIDCProvider, secrets: DfeSecrets | None) -> GraphCredentials:
    """Resolve the Graph API credentials for an Entra provider.

    The tenant id comes from its value, then its env var. The client id is the login client's. The client secret is the directory one, falling back to the login client secret, because both belong to the same app registration.
    """
    groups = provider.groups
    directory_secret = resolve_credential(
        env_name=groups.client_secret_env, secret_path=groups.client_secret_path, secrets=secrets
    )
    client_secret = (directory_secret) or (
        resolve_credential(
            env_name=provider.client_secret_env,
            secret_path=provider.client_secret_path,
            secrets=secrets,
        )
    )
    return GraphCredentials(
        client_id=resolve_credential(
            env_name=provider.client_id_env, secrets=secrets, value=provider.client_id
        ),
        client_secret=client_secret,
        tenant_id=resolve_credential(
            env_name=groups.tenant_id_env, secrets=secrets, value=groups.tenant_id
        ),
    )


class EntraAdapter(OIDCGroupAdapter):
    """Entra ID group adapter using the Microsoft Graph API.

    The login-path methods fail open: missing credentials or API errors log a warning and return an unfriendly fallback rather than propagating exceptions. This keeps authentication working even when the group resolution API is unreachable. ``list_all_groups`` raises :class:`DirectoryError` instead, so a group sync reports the failure.
    """

    GRAPH_BASE = "https://graph.microsoft.com/v1.0"
    _GRAPH_SCOPE = "https://graph.microsoft.com/.default"
    _PAGE_SIZE = 999  # Maximum $top value accepted by Graph API

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def resolve_groups(self, group_ids: list[str]) -> dict[str, str]:
        """Resolve Entra group GUIDs to display names via Graph API.

        Fetches each group individually using ``GET /v1.0/groups/{id}``.
        For groups that cannot be resolved the original GUID is used as the
        display name (unfriendly fallback).

        Args:
            group_ids: List of Entra group object IDs (GUIDs).

        Returns:
            Mapping of group ID -> display name.  Falls back to
            ``{id: id}`` when credentials are missing or the API fails.
        """
        if not group_ids:
            return {}

        token = self._get_token()
        if token is None:
            logger.warning(
                "Entra resolve_groups: no token available -- returning unfriendly fallback",
                provider=self._provider.issuer,
                group_count=len(group_ids),
            )
            return {gid: gid for gid in group_ids}

        from scalo.http import AsyncHttpClient

        result: dict[str, str] = {}
        headers = {"Authorization": f"Bearer {token}"}

        async with AsyncHttpClient() as client:
            for gid in group_ids:
                url = f"{self.GRAPH_BASE}/groups/{gid}?$select=displayName,mail"
                try:
                    response = await client.get(url, headers=headers)
                    data = response.json()
                    display_name = data.get("displayName") or gid
                    result[gid] = display_name
                except Exception as exc:
                    logger.warning(
                        "Entra resolve_groups: failed to resolve group",
                        group_id=gid,
                        error=describe_error(exc=exc, read_summary=_graph_error_summary),
                    )
                    result[gid] = gid

        return result

    async def resolve_user_groups(self, directory_id: str) -> list[GroupInfo]:
        """Enumerate a user's group memberships via Graph transitiveMemberOf.

        This is the >200 group OVERAGE path: when a user is in too many groups,
        Entra drops the ``groups`` array from the id_token and emits a
        ``_claim_names`` pointer instead, so the RP must fetch the membership
        itself. Pages ``GET /users/{id}/transitiveMemberOf/microsoft.graph.group``
        (the OData cast returns groups only, never directory roles), following
        ``@odata.nextLink``.

        ``directory_id`` MUST be the Entra object id (the ``oid`` claim), not the
        pairwise ``sub`` - Graph keys ``/users/{id}`` on the object id.

        Fails open: no token or an API error returns ``[]`` (default deny), never
        raises, so an enrichment outage cannot break login or over-grant.
        """
        if not directory_id:
            return []

        token = self._get_token()
        if token is None:
            logger.warning(
                "Entra resolve_user_groups: no token available -- returning empty list",
                provider=self._provider.issuer,
            )
            return []

        from scalo.http import AsyncHttpClient

        groups: list[GroupInfo] = []
        headers = {"Authorization": f"Bearer {token}"}
        url = (
            f"{self.GRAPH_BASE}/users/{directory_id}/transitiveMemberOf/microsoft.graph.group"
            f"?$select=id,displayName,mail,description&$top={self._PAGE_SIZE}"
        )

        async with AsyncHttpClient() as client:
            while url:
                try:
                    response = await client.get(url, headers=headers)
                    data = response.json()
                except Exception as exc:
                    logger.warning(
                        "Entra resolve_user_groups: API call failed",
                        directory_id=directory_id,
                        error=describe_error(exc=exc, read_summary=_graph_error_summary),
                    )
                    break

                for item in data.get("value", []):
                    groups.append(
                        GroupInfo(
                            id=item.get("id", ""),
                            name=item.get("displayName", ""),
                            email=item.get("mail", "") or "",
                            description=item.get("description", "") or "",
                        )
                    )

                url = data.get("@odata.nextLink", "")

        return groups

    async def list_all_groups(self) -> list[GroupInfo]:
        """List every group in the tenant via Graph, following ``@odata.nextLink`` until all pages are consumed.

        Raises :class:`DirectoryError` when a credential is not configured, Graph issues no token or any page fails, so no partial listing is returned.
        """
        provider_type = self._provider.type
        credentials = graph_credentials(provider=self._provider, secrets=self._secrets)
        missing = _missing_graph_settings(credentials=credentials)
        if missing:
            detail = f"credentials not configured, missing {', '.join(missing)}"
            raise DirectoryError(detail=detail, provider_type=provider_type)
        token = self._get_token()
        if token is None:
            detail = "Microsoft Graph issued no token for the app registration; the reason is in the engine log"
            raise DirectoryError(detail=detail, provider_type=provider_type)
        url = (
            f"{self.GRAPH_BASE}/groups"
            f"?$select=id,displayName,mail,description&$top={self._PAGE_SIZE}"
        )
        return await self._list_graph_groups(token=token, url=url)

    async def test_connection(self) -> tuple[bool, str]:
        """Test Graph API connectivity with a minimal query.

        Issues ``GET /v1.0/groups?$top=1`` to verify credentials and
        network access without fetching a full result set.

        Returns:
            ``(True, message)`` on success, ``(False, message)`` on failure.
        """
        credentials = graph_credentials(provider=self._provider, secrets=self._secrets)
        missing = _missing_graph_settings(credentials=credentials)
        if missing:
            return (False, f"Entra credentials not configured, missing {', '.join(missing)}")
        token = self._get_token()
        if token is None:
            return (
                False,
                "Microsoft Graph issued no token for the app registration; the reason is in the "
                "engine log",
            )

        return await self._check_graph(
            token=token, url=f"{self.GRAPH_BASE}/groups?$top=1&$select=id"
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _check_graph(self, *, token: str, url: str) -> tuple[bool, str]:
        """Read the first page of a Graph group listing at *url* and report whether it answered with a group list."""
        from scalo.http import AsyncHttpClient

        provider_type = self._provider.type
        try:
            async with AsyncHttpClient() as client:
                body, _response_headers = await fetch_directory_page(
                    client=client,
                    headers={"Authorization": f"Bearer {token}"},
                    provider_type=provider_type,
                    read_summary=_graph_error_summary,
                    url=url,
                )
            groups, _next_url = _graph_page(body=body, provider_type=provider_type, url=url)
        except DirectoryError as exc:
            logger.warning("Entra test_connection: Graph API call failed", error=exc.detail)
            return (False, f"Graph API connection failed: {exc.detail}")
        except Exception as exc:
            # An unexpected error's text could quote the request's token, so only its type is reported.
            logger.error(
                "Entra test_connection: Graph API call failed unexpectedly",
                error_type=type(exc).__name__,
                frames=error_frames(exc=exc),
            )
            return (False, f"Graph API connection failed: {type(exc).__name__}")
        return (True, f"Graph API connection successful -- {len(groups)} group(s) returned")

    async def _list_graph_groups(self, *, token: str, url: str) -> list[GroupInfo]:
        """Page a Graph group listing from *url*, raising :class:`DirectoryError` when any page fails."""
        from scalo.http import AsyncHttpClient

        provider_type = self._provider.type
        groups = []
        headers = {"Authorization": f"Bearer {token}"}
        async with AsyncHttpClient() as client:
            while url:
                body, _response_headers = await fetch_directory_page(
                    client=client,
                    headers=headers,
                    provider_type=provider_type,
                    read_summary=_graph_error_summary,
                    url=url,
                )
                page, url = _graph_page(body=body, provider_type=provider_type, url=url)
                groups.extend(page)
        return groups

    def _get_token(self) -> str | None:
        """Acquire an OAuth2 access token via MSAL client credentials flow.

        Credentials resolve through :func:`graph_credentials`. Returns None if any credential is missing or if MSAL fails.

        Returns:
            Access token string, or None if credentials are unavailable.
        """
        credentials = graph_credentials(provider=self._provider, secrets=self._secrets)
        if (
            not (credentials.tenant_id)
            or not (credentials.client_id)
            or not (credentials.client_secret)
        ):
            return None

        authority = f"https://login.microsoftonline.com/{credentials.tenant_id}"
        try:
            app = msal.ConfidentialClientApplication(
                authority=authority,
                client_credential=credentials.client_secret,
                client_id=credentials.client_id,
            )
            result = app.acquire_token_for_client(scopes=[self._GRAPH_SCOPE])
        except Exception as exc:
            logger.warning(
                "Entra _get_token: MSAL token acquisition failed",
                error=describe_error(exc=exc),
                tenant_id=credentials.tenant_id,
            )
            return None

        token = result.get("access_token")
        if not token:
            error_desc = str(result.get("error_description", result.get("error", "unknown")))
            logger.warning(
                "Entra _get_token: no access_token in MSAL response",
                error=error_desc[:DIRECTORY_SUMMARY_LIMIT],
            )
            return None

        return token
