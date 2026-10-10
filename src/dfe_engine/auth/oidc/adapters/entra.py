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

Credentials resolve through :func:`graph_credentials`. If any credential is missing the adapter fails open: all methods return unfriendly fallbacks rather than raising.

Usage::

    from dfe_engine.auth.oidc.adapters.entra import EntraAdapter

    adapter = EntraAdapter(provider, secrets=store)
    groups = await adapter.list_all_groups()
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

import msal
from scalo.logger import logger

from dfe_engine.auth.oidc.adapters.base import OIDCGroupAdapter
from dfe_engine.auth.oidc.credential_env import resolve_credential
from dfe_engine.auth.oidc.models import GroupInfo

if TYPE_CHECKING:
    from dfe_engine.auth.oidc.models import OIDCProvider
    from dfe_engine.secrets import DfeSecrets


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

    All public methods fail open -- missing credentials or API errors log a
    warning and return an unfriendly fallback rather than propagating
    exceptions.  This keeps authentication working even when the group
    resolution API is unreachable.
    """

    GRAPH_BASE = "https://graph.microsoft.com/v1.0"
    _GRAPH_SCOPE = "https://graph.microsoft.com/.default"
    _PAGE_SIZE = 999  # Maximum $top value accepted by Graph API

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

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
                "Entra list_all_groups: no token available -- returning empty list",
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
                "Entra credentials not configured - set groups.tenant_id, the client id "
                "and a client secret (groups.client_secret, else the login client secret)",
            )

        from scalo.http import AsyncHttpClient

        url = f"{self.GRAPH_BASE}/groups?$top=1&$select=id"
        headers = {"Authorization": f"Bearer {token}"}

        async with AsyncHttpClient() as client:
            try:
                response = await client.get(url, headers=headers)
                data = response.json()
                count = len(data.get("value", []))
                return (True, f"Graph API connection successful -- {count} group(s) returned")
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
                tenant_id=credentials.tenant_id,
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
