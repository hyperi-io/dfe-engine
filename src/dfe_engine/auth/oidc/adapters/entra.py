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

Credential env vars are referenced by name from ``OIDCProvider.client_id_env``
and ``GroupResolutionConfig.tenant_id_env`` / ``client_secret_env``.  If any
credential is missing the adapter fails open — all methods return unfriendly
fallbacks rather than raising.

Usage::

    from dfe_engine.auth.oidc.adapters.entra import EntraAdapter
    from dfe_engine.auth.oidc.models import OIDCProvider

    adapter = EntraAdapter(provider)
    groups = await adapter.list_all_groups()
    display_names = await adapter.resolve_groups(group_ids)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import msal
from scalo.logger import logger

from dfe_engine.auth.oidc.adapters.base import OIDCGroupAdapter
from dfe_engine.auth.oidc.models import GroupInfo
from dfe_engine.env_refs import resolve_env_ref

if TYPE_CHECKING:
    from dfe_engine.auth.oidc.models import OIDCProvider


class EntraAdapter(OIDCGroupAdapter):
    """Entra ID group adapter using the Microsoft Graph API.

    All public methods fail open — missing credentials or API errors log a
    warning and return an unfriendly fallback rather than propagating
    exceptions.  This keeps authentication working even when the group
    resolution API is unreachable.
    """

    GRAPH_BASE = "https://graph.microsoft.com/v1.0"
    _GRAPH_SCOPE = "https://graph.microsoft.com/.default"
    _PAGE_SIZE = 999  # Maximum $top value accepted by Graph API

    def __init__(self, provider: OIDCProvider) -> None:
        super().__init__(provider)

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
            Mapping of group ID → display name.  Falls back to
            ``{id: id}`` when credentials are missing or the API fails.
        """
        if not group_ids:
            return {}

        token = self._get_token()
        if token is None:
            logger.warning(
                "Entra resolve_groups: no token available — returning unfriendly fallback",
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
                        error=str(exc),
                    )
                    result[gid] = gid

        return result

    async def list_all_groups(self) -> list[GroupInfo]:
        """List all groups in the tenant via Graph API.

        Pages through ``GET /v1.0/groups?$select=id,displayName,mail,description``
        following ``@odata.nextLink`` until all pages are consumed.

        Returns:
            List of GroupInfo objects.  Returns an empty list when credentials
            are missing or the API call fails.
        """
        token = self._get_token()
        if token is None:
            logger.warning(
                "Entra list_all_groups: no token available — returning empty list",
                provider=self._provider.issuer,
            )
            return []

        from scalo.http import AsyncHttpClient

        groups: list[GroupInfo] = []
        headers = {"Authorization": f"Bearer {token}"}
        url = (
            f"{self.GRAPH_BASE}/groups"
            f"?$select=id,displayName,mail,description&$top={self._PAGE_SIZE}"
        )

        async with AsyncHttpClient() as client:
            while url:
                try:
                    response = await client.get(url, headers=headers)
                    data = response.json()
                except Exception as exc:
                    logger.warning(
                        "Entra list_all_groups: API call failed",
                        url=url,
                        error=str(exc),
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

    async def test_connection(self) -> tuple[bool, str]:
        """Test Graph API connectivity with a minimal query.

        Issues ``GET /v1.0/groups?$top=1`` to verify credentials and
        network access without fetching a full result set.

        Returns:
            ``(True, message)`` on success, ``(False, message)`` on failure.
        """
        token = self._get_token()
        if token is None:
            return (
                False,
                "Entra credentials not configured — set the env vars for "
                "tenant_id, client_id, and client_secret",
            )

        from scalo.http import AsyncHttpClient

        url = f"{self.GRAPH_BASE}/groups?$top=1&$select=id"
        headers = {"Authorization": f"Bearer {token}"}

        async with AsyncHttpClient() as client:
            try:
                response = await client.get(url, headers=headers)
                data = response.json()
                count = len(data.get("value", []))
                return (True, f"Graph API connection successful — {count} group(s) returned")
            except Exception as exc:
                logger.warning(
                    "Entra test_connection: Graph API call failed",
                    error=str(exc),
                )
                return (False, f"Graph API connection failed: {exc}")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_token(self) -> str | None:
        """Acquire an OAuth2 access token via MSAL client credentials flow.

        Reads credential values from the env vars whose *names* are stored in
        the provider configuration.  Returns None if any credential is missing
        or if MSAL fails to acquire a token.

        Returns:
            Access token string, or None if credentials are unavailable.
        """
        # Read env var names from provider config
        tenant_id_env = self._provider.groups.tenant_id_env
        client_secret_env = self._provider.groups.client_secret_env
        client_id_env = self._provider.client_id_env

        # Resolve actual values from environment (None when unconfigured or unset)
        tenant_id = resolve_env_ref(tenant_id_env, default=None)
        client_id = resolve_env_ref(client_id_env, default=None)
        client_secret = resolve_env_ref(client_secret_env, default=None)

        if not tenant_id or not client_id or not client_secret:
            return None

        authority = f"https://login.microsoftonline.com/{tenant_id}"
        try:
            app = msal.ConfidentialClientApplication(
                client_id=client_id,
                client_credential=client_secret,
                authority=authority,
            )
            result = app.acquire_token_for_client(scopes=[self._GRAPH_SCOPE])
        except Exception as exc:
            logger.warning(
                "Entra _get_token: MSAL token acquisition failed",
                tenant_id=tenant_id,
                error=str(exc),
            )
            return None

        token = result.get("access_token")
        if not token:
            error_desc = result.get("error_description", result.get("error", "unknown"))
            logger.warning(
                "Entra _get_token: no access_token in MSAL response",
                error=error_desc,
            )
            return None

        return token
