#  Project:      dfe-engine
#  File:         auth/oidc/adapters/entra.py
#  Purpose:      Microsoft Entra ID adapter for Graph API group resolution
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Microsoft Entra ID (Azure AD) OIDC group adapter.

Resolves a user's groups and group GUIDs to display names via the Microsoft
Graph API: with the user's own login token where it can, otherwise with the
app registration's own credentials and its admin-consented application
permission.

Credentials resolve through :func:`graph_credentials`. ``resolve_user_groups`` and ``test_connection`` fail open, returning fallbacks rather than raising; ``list_all_groups`` raises.

Usage::

    from dfe_engine.auth.oidc.adapters.entra import EntraAdapter

    adapter = EntraAdapter(provider, secrets=store)
    groups = await adapter.list_all_groups()
"""

import functools
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import msal
import requests
from requests.adapters import HTTPAdapter
from scalo.logger import logger

from dfe_engine.auth.oidc.adapters.base import (
    DirectoryCredentialError,
    DirectoryCredentialUnreadableError,
    DirectoryNotConfiguredError,
    DirectoryPageError,
    DirectoryPageLimitError,
    OIDCGroupAdapter,
)
from dfe_engine.auth.oidc.credential_env import (
    ResolvedCredential,
    resolve_credential,
    unreadable_credential_message,
)
from dfe_engine.auth.oidc.models import GroupInfo
from dfe_engine.http_errors import (
    credential_failure_message,
    describe_http_error,
    http_failure_message,
    provider_error_code,
)

if TYPE_CHECKING:
    from dfe_engine.auth.oidc.models import OIDCProvider
    from dfe_engine.secrets import DfeSecrets


@dataclass(frozen=True, slots=True)
class GraphCredentials:
    """The app-only credentials a Graph API token is requested with; an empty field was not configured.

    Attributes:
        client_id: The login client's id.
        client_secret: The directory client secret, else the login client secret.
        tenant_id: The Entra tenant.
        secret_unreadable: No client secret resolved and the secret store, asked
            for one, did not answer.
    """

    client_id: str
    client_secret: str
    tenant_id: str
    secret_unreadable: bool = False

    @property
    def configured(self) -> bool:
        """Whether all three are set, which is what a token request needs."""
        return bool(self.tenant_id and self.client_id and self.client_secret)


def graph_credentials(*, provider: OIDCProvider, secrets: DfeSecrets | None) -> GraphCredentials:
    """Resolve the Graph API credentials for an Entra provider.

    The tenant id comes from its value, then its env var. The client id is the login client's. The client secret is the directory one, falling back to the login client secret, because both belong to the same app registration.
    """
    groups = provider.groups
    directory_secret = resolve_credential(
        env_name=groups.client_secret_env, secret_path=groups.client_secret_path, secrets=secrets
    )
    login_secret = (
        ResolvedCredential()
        if directory_secret.value
        else resolve_credential(
            env_name=provider.client_secret_env,
            secret_path=provider.client_secret_path,
            secrets=secrets,
        )
    )
    client_secret = directory_secret.value or login_secret.value
    return GraphCredentials(
        client_id=resolve_credential(
            env_name=provider.client_id_env, secrets=secrets, value=provider.client_id
        ).value,
        client_secret=client_secret,
        secret_unreadable=not client_secret
        and (directory_secret.unreadable or login_secret.unreadable),
        tenant_id=resolve_credential(
            env_name=groups.tenant_id_env, secrets=secrets, value=groups.tenant_id
        ).value,
    )


class _EntraHttpClient:
    """The HTTP client MSAL reaches Entra through, raising for a throttled or failing answer.

    MSAL parses any token answer under 500 as the token response and keeps no status,
    so a 429 would come back as an error body that reads like a refused credential. A
    429 or a 5xx raises ``requests.HTTPError`` here instead, carrying the response.
    """

    def __init__(self, session: requests.Session) -> None:
        """Send through ``session``."""
        self._session = session

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        """GET ``url``, raising for a 429 or a 5xx."""
        return self._checked(self._session.get(url, **kwargs))

    def post(self, url: str, **kwargs: Any) -> requests.Response:
        """POST to ``url``, raising for a 429 or a 5xx."""
        return self._checked(self._session.post(url, **kwargs))

    def close(self) -> None:
        """Close the session."""
        self._session.close()

    @staticmethod
    def _checked(response: requests.Response) -> requests.Response:
        if response.status_code == 429 or response.status_code >= 500:
            response.raise_for_status()
        return response


class EntraAdapter(OIDCGroupAdapter):
    """Entra ID group adapter using the Microsoft Graph API.

    ``resolve_user_groups`` and ``test_connection`` fail open -- missing
    credentials or API errors log a warning and return an unfriendly fallback
    rather than propagating exceptions.  This keeps authentication working even
    when the group resolution API is unreachable.  ``list_all_groups`` is not on
    the login path and raises when the credentials are not configured or cannot be
    read, when Entra issues no token for them or cannot be reached, and when Graph
    fails mid-listing.
    """

    GRAPH_BASE = "https://graph.microsoft.com/v1.0"
    _GRAPH_SCOPE = "https://graph.microsoft.com/.default"
    _PAGE_SIZE = 999  # Maximum $top value accepted by Graph API
    _GROUP_QUERY = f"transitiveMemberOf/microsoft.graph.group?$select=id,displayName,mail,description&$top={_PAGE_SIZE}"

    def __init__(
        self,
        provider: OIDCProvider,
        *,
        access_token: str = "",
        secrets: DfeSecrets | None = None,
    ) -> None:
        """Keep the login's access token, which Graph reads the user's own groups with."""
        super().__init__(provider, secrets=secrets)
        self._access_token = access_token

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def resolve_user_groups(self, directory_id: str) -> list[GroupInfo]:
        """Enumerate a user's group memberships via Graph transitiveMemberOf.

        This is the >200 group OVERAGE path: when a user is in too many groups,
        Entra drops the ``groups`` array from the id_token and emits a
        ``_claim_names`` pointer instead, so the RP must fetch the membership
        itself. The OData cast returns groups only, never directory roles, and
        every ``@odata.nextLink`` page is followed.

        The user's own login token asks ``/me`` first. Graph answers that with
        User.Read; without GroupMember.Read.All each group carries only its id,
        which is all role resolution needs. A token Graph refuses falls through
        to the app-only credentials, which ask ``/users/{directory_id}``. A user
        token that answers with no groups is the answer and does not fall through.

        ``directory_id`` MUST be the Entra object id (the ``oid`` claim), not the
        pairwise ``sub`` - Graph keys ``/users/{id}`` on the object id.

        Fails open: no token or an API error returns ``[]`` (default deny), never
        raises, so an enrichment outage cannot break login or over-grant.
        """
        if not directory_id:
            return []

        if self._access_token:
            groups = await self._page_groups(
                f"{self.GRAPH_BASE}/me/{self._GROUP_QUERY}", self._access_token
            )
            if groups is not None:
                return groups

        try:
            token = self._get_token()
        except Exception as exc:
            logger.warning(
                "Entra resolve_user_groups: token request failed -- returning empty list",
                operation="token",
                provider=self._provider.issuer,
                **describe_http_error(exc),
            )
            return []
        if token is None:
            logger.warning(
                "Entra resolve_user_groups: no token available -- returning empty list",
                provider=self._provider.issuer,
            )
            return []
        groups = await self._page_groups(
            f"{self.GRAPH_BASE}/users/{directory_id}/{self._GROUP_QUERY}", token
        )
        return groups or []

    async def _page_groups(self, url: str, token: str) -> list[GroupInfo] | None:
        """Every page of one transitiveMemberOf listing, or None when Graph did not answer.

        A listing still paging at :attr:`MAX_LOGIN_PAGES` is no answer either.
        """
        from scalo.http import AsyncHttpClient

        groups: list[GroupInfo] = []
        headers = {"Authorization": f"Bearer {token}"}
        async with AsyncHttpClient() as client:
            for _page in range(self.MAX_LOGIN_PAGES):
                try:
                    response = await client.get(url, headers=headers)
                    data = response.json()
                except Exception as exc:
                    logger.warning(
                        "Entra resolve_user_groups: API call failed",
                        operation="resolve_user_groups",
                        provider=self._provider.issuer,
                        **describe_http_error(exc),
                    )
                    return None
                if not isinstance(data, dict):
                    return None
                # A token that may not read a group still gets it, with only the id set and
                # every other property null; the id is all role resolution keys on.
                groups.extend(
                    GroupInfo(
                        id=str(item["id"]),
                        name=item.get("displayName") or "",
                        email=item.get("mail") or "",
                        description=item.get("description") or "",
                    )
                    for item in data.get("value") or []
                    if isinstance(item, dict) and item.get("id")
                )
                url = data.get("@odata.nextLink", "")
                if not url:
                    return groups
        logger.warning(
            "Entra resolve_user_groups: Graph still linked a next page at the page limit",
            operation="resolve_user_groups",
            pages=self.MAX_LOGIN_PAGES,
            provider=self._provider.issuer,
        )
        return None

    async def list_all_groups(self) -> list[GroupInfo]:
        """List all groups in the tenant via Graph API.

        Pages through ``GET /v1.0/groups?$select=id,displayName,mail,description``
        following ``@odata.nextLink`` until all pages are consumed.

        Returns:
            List of GroupInfo objects.

        Raises:
            DirectoryNotConfiguredError: The tenant id, client id or client
                secret is not configured.
            DirectoryCredentialUnreadableError: No client secret resolved because
                the secret store did not answer.
            DirectoryCredentialError: The credentials are set but Entra issues
                no token for them. ``_get_token`` logs why.
            DirectoryPageError: A page Graph served carried no ``value`` list.
            DirectoryPageLimitError: Graph still linked a next page at the page limit.
            Exception: Whatever MSAL raised when Entra could not be reached or
                failed, and whatever the client raised on a page Graph did not
                serve. A listing cut short is never returned: group sync would
                record the groups read so far as the whole tenant.
        """
        token = self._get_token()
        if token is None:
            credentials = self._graph_credentials
            if credentials.configured:
                raise DirectoryCredentialError
            if credentials.secret_unreadable:
                raise DirectoryCredentialUnreadableError
            raise DirectoryNotConfiguredError

        from scalo.http import AsyncHttpClient

        groups: list[GroupInfo] = []
        unidentified = 0
        headers = {"Authorization": f"Bearer {token}"}
        url = (
            f"{self.GRAPH_BASE}/groups"
            f"?$select=id,displayName,mail,description&$top={self._PAGE_SIZE}"
        )

        async with AsyncHttpClient() as client:
            for _page in range(self.MAX_LISTING_PAGES):
                response = await client.get(url, headers=headers)
                data = response.json()
                if not isinstance(data, dict) or not isinstance(data.get("value"), list):
                    raise DirectoryPageError
                for item in data["value"]:
                    # A group with no id can never be linked, so it is not handed to the sync.
                    if not item.get("id"):
                        unidentified += 1
                        continue
                    groups.append(
                        GroupInfo(
                            id=item["id"],
                            name=item.get("displayName") or "",
                            email=item.get("mail", "") or "",
                            description=item.get("description", "") or "",
                        )
                    )
                url = data.get("@odata.nextLink")
                if not url:
                    if unidentified:
                        logger.warning(
                            "Entra list_all_groups skipped groups with no id",
                            count=unidentified,
                            provider=self._provider.issuer,
                        )
                    return groups
        raise DirectoryPageLimitError(self.MAX_LISTING_PAGES)

    async def test_connection(self) -> tuple[bool, str]:
        """Test Graph API connectivity with a minimal query.

        Issues ``GET /v1.0/groups?$top=1`` to verify credentials and
        network access without fetching a full result set.

        Returns:
            ``(True, message)`` on success, ``(False, message)`` on failure. A
            credential that is not set, one the secret store did not answer for,
            one Entra issues no token for and an Entra that could not be reached
            each read differently, as they do in group sync.
        """
        try:
            token = self._get_token()
        except Exception as exc:
            logger.warning(
                "Entra test_connection: token request failed",
                operation="token",
                provider=self._provider.issuer,
                **describe_http_error(exc),
            )
            return False, http_failure_message(exc, service="Entra ID")
        if token is None:
            credentials = self._graph_credentials
            if credentials.configured:
                return False, credential_failure_message(service="Graph API")
            if credentials.secret_unreadable:
                return False, unreadable_credential_message(credential="Entra client secret")
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
                    operation="test_connection",
                    provider=self._provider.issuer,
                    **describe_http_error(exc),
                )
                return (False, http_failure_message(exc, service="Graph API"))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @functools.cached_property
    def _graph_credentials(self) -> GraphCredentials:
        """The Graph credentials, resolved once per adapter.

        An adapter serves one sync, one connection test or one login, so an
        unreachable secret store is asked and logged once.
        """
        return graph_credentials(provider=self._provider, secrets=self._secrets)

    def _msal_http_client(self) -> _EntraHttpClient:
        """A fresh client for one token request, retrying once as MSAL's own default client does.

        The Entra authority is always https, so only https gets the retrying adapter.
        """
        session = requests.Session()
        session.mount("https://", HTTPAdapter(max_retries=1))
        return _EntraHttpClient(session)

    def _get_token(self) -> str | None:
        """Acquire an OAuth2 access token via MSAL client credentials flow.

        Credentials resolve through :func:`graph_credentials`.

        Returns:
            The access token, or None when a credential is missing or Entra
            refuses the credentials or the tenant.

        Raises:
            Exception: Whatever was raised when Entra could not be reached, throttled
                the request or failed on its side: a ``requests`` connection error
                for no answer, ``requests.HTTPError`` for a 429 or a 5xx, a
                ``JSONDecodeError`` for a token answer that is not JSON.
        """
        credentials = self._graph_credentials
        if not credentials.configured:
            return None

        authority = f"https://login.microsoftonline.com/{credentials.tenant_id}"
        try:
            app = msal.ConfidentialClientApplication(
                authority=authority,
                client_credential=credentials.client_secret,
                client_id=credentials.client_id,
                http_client=self._msal_http_client(),
            )
            result = app.acquire_token_for_client(scopes=[self._GRAPH_SCOPE])
        except json.JSONDecodeError:
            # A token answer that is not JSON is Entra failing, not refusing the credentials.
            raise
        except ValueError as exc:
            # MSAL raises ValueError for an authority it cannot use, such as a tenant Entra does not know.
            logger.warning(
                "Entra _get_token: MSAL refused the authority",
                operation="token",
                provider=self._provider.issuer,
                tenant_id=credentials.tenant_id,
                **describe_http_error(exc),
            )
            return None

        token = result.get("access_token")
        if not token:
            # The AADSTS numbers say what Entra refused; its description is prose and stays out.
            error_codes = [c for c in result.get("error_codes") or [] if isinstance(c, int)]
            logger.warning(
                "Entra _get_token: no access_token in MSAL response",
                code=provider_error_code(result) or "unknown",
                error_codes=error_codes,
                operation="token",
                provider=self._provider.issuer,
                tenant_id=credentials.tenant_id,
            )
            return None

        return token
