#  Project:      DFE Engine
#  File:         src/dfe_engine/auth/oidc/adapters/google.py
#  Purpose:      Google Workspace Admin SDK adapter for OIDC group resolution.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Google Workspace group adapter.

Google puts no groups in its tokens, so a login's groups come from a directory
lookup. By default that lookup runs as the user: the login asks for the
``cloud-identity.groups.readonly`` scope and the adapter reads the user's own
groups from the Cloud Identity Groups API with their access token. That needs
nothing from the customer's Workspace admin.

A service account is optional. When ``service_account_json`` is configured, the
service account reads the Admin SDK Directory API as itself, never as an
impersonated user, which needs the org's admin to have assigned it a groups
admin role. It runs the group sync and the connection test, and answers a login
the user's token could not.

Both paths name a group by its directory id: the Directory API ``id``, and the
Cloud Identity resource name with its ``groups/`` prefix removed. Group files
link on that id, never on the group email, which an admin can rename.
"""

import asyncio
import json
from typing import TYPE_CHECKING, Any

import httpx
from scalo.logger import logger

from dfe_engine.auth.oidc.adapters.base import OIDCGroupAdapter
from dfe_engine.auth.oidc.credential_env import resolve_credential
from dfe_engine.auth.oidc.idp_errors import describe_idp_error, idp_failure_message
from dfe_engine.auth.oidc.models import GroupInfo, OIDCProvider

if TYPE_CHECKING:
    from dfe_engine.secrets import DfeSecrets

# Google Admin SDK scope for read-only group directory access.
_DIRECTORY_SCOPE = "https://www.googleapis.com/auth/admin.directory.group.readonly"

_GROUP_RESOURCE_PREFIX = "groups/"

# Every Google group carries this label, and Cloud Identity refuses a membership search with no label in its query.
_GOOGLE_GROUP_LABEL = "cloudidentity.googleapis.com/groups.discussion_forum"

# Pages read per search at most, so a server that keeps handing back a page token cannot hold a login.
_MAX_PAGES = 50


def _cel_string(value: str) -> str:
    """*value* as a single-quoted CEL string literal; an email may legally hold a quote."""
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def _group_from_relation(item: object) -> GroupInfo | None:
    """The group one Cloud Identity search result names, or None when it carries no ``groups/<id>`` name."""
    if not isinstance(item, dict):
        return None
    resource = str(item.get("group") or "")
    group_id = resource.removeprefix(_GROUP_RESOURCE_PREFIX)
    if group_id == resource or not group_id or "/" in group_id:
        return None
    group_key = item.get("groupKey")
    email = str(group_key.get("id") or "") if isinstance(group_key, dict) else ""
    return GroupInfo(
        description=str(item.get("description") or ""),
        email=email,
        id=group_id,
        name=str(item.get("displayName") or email or group_id),
    )


class GoogleAdapter(OIDCGroupAdapter):
    """Google Workspace groups: Cloud Identity as the user, else the Directory API as a service account.

    Every method fails closed: missing credentials or a refused call yield no
    groups, never an exception.
    """

    CLOUD_IDENTITY_BASE = "https://cloudidentity.googleapis.com/v1"

    def __init__(
        self,
        provider: OIDCProvider,
        *,
        access_token: str = "",
        secrets: DfeSecrets | None = None,
    ) -> None:
        """Keep the login's access token, which Cloud Identity reads the user's own groups with."""
        super().__init__(provider, secrets=secrets)
        self._access_token = access_token

    # ------------------------------------------------------------------
    # Public async interface
    # ------------------------------------------------------------------

    async def resolve_user_groups(self, directory_id: str) -> list[GroupInfo]:
        """Return the groups the user with email *directory_id* belongs to.

        Asks Cloud Identity with the user's own access token first. Only when
        that cannot answer - no token, or the call was refused or failed - does
        a configured service account ask the Directory API. A user token that
        answers with no groups is the answer: it does not fall through.

        Returns:
            The user's groups keyed by directory id, or ``[]`` (default deny)
            when no path could answer.
        """
        if not directory_id:
            return []
        if self._access_token:
            groups = await self._groups_with_user_token(email=directory_id)
            if groups is not None:
                return groups
        return await self._groups_with_service_account(user_key=directory_id)

    async def list_all_groups(self) -> list[GroupInfo]:
        """List every group in ``groups.domain`` through the service account.

        A user token reaches only that user's own groups, so without a service
        account there is nothing to enumerate: link group files by directory id
        instead.

        Returns:
            List of GroupInfo, or an empty list when no service account is
            configured or the API call fails.
        """
        service = self._get_service()
        if service is None:
            return []

        try:
            raw = await asyncio.to_thread(self._fetch_all_groups_sync, service)
        except Exception as exc:
            logger.warning(
                "Google Admin SDK list_all_groups failed",
                operation="list_all_groups",
                provider=self._provider.issuer,
                **describe_idp_error(exc),
            )
            return []

        return [GroupInfo(id=g["id"], name=g["name"], email=g.get("email", "")) for g in raw]

    async def test_connection(self) -> tuple[bool, str]:
        """Probe the Directory API as the service account, when one is configured.

        Returns:
            (True, message) when the probe succeeds or there is no service
            account to probe, else (False, error_message).
        """
        groups = self._provider.groups
        if not (groups.service_account_json_path) and not (groups.service_account_json_env):
            return True, (
                "No service account configured: each login reads that user's groups "
                "from Cloud Identity with the user's own token, so there is no "
                "directory credential to test"
            )
        if not self._service_account_json():
            return False, (
                "A service account is configured but resolves to nothing: send it as "
                "'groups.service_account_json' to the provider API, or set the env var "
                "named in groups.service_account_json_env"
            )
        service = self._get_service()
        if service is None:
            return False, "Failed to build service from the configured service account JSON"

        try:
            await asyncio.to_thread(self._probe_sync, service)
            return True, "OK"
        except Exception as exc:
            logger.warning(
                "Google Admin SDK test_connection failed",
                operation="test_connection",
                provider=self._provider.issuer,
                **describe_idp_error(exc),
            )
            return False, idp_failure_message(exc, service="Google Admin SDK")

    # ------------------------------------------------------------------
    # Cloud Identity, as the user
    # ------------------------------------------------------------------

    async def _groups_with_user_token(self, *, email: str) -> list[GroupInfo] | None:
        """The user's groups from Cloud Identity, or None when Cloud Identity could not answer.

        Transitive search also names the groups the user holds through nesting,
        but only Enterprise and Cloud Identity Premium editions serve it, so its
        403 falls back to direct groups.
        """
        from scalo.http import AsyncHttpClient

        query = f"member_key_id == {_cel_string(email)} && '{_GOOGLE_GROUP_LABEL}' in labels"
        headers = {"Authorization": f"Bearer {self._access_token}"}
        async with AsyncHttpClient() as client:
            try:
                return await self._search(
                    client, headers=headers, method="searchTransitiveGroups", query=query
                )
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 403:
                    self._log_refusal(exc=exc, method="searchTransitiveGroups")
                    return None
                logger.info(
                    "Google Cloud Identity: transitive group search refused, reading direct groups",
                    operation="searchTransitiveGroups",
                    provider=self._provider.issuer,
                    **describe_idp_error(exc),
                )
            except (httpx.HTTPError, ValueError) as exc:
                self._log_failure(exc=exc, method="searchTransitiveGroups")
                return None

            try:
                return await self._search(
                    client, headers=headers, method="searchDirectGroups", query=query
                )
            except httpx.HTTPStatusError as exc:
                self._log_refusal(exc=exc, method="searchDirectGroups")
            except (httpx.HTTPError, ValueError) as exc:
                self._log_failure(exc=exc, method="searchDirectGroups")
        return None

    async def _search(
        self, client: Any, *, headers: dict[str, str], method: str, query: str
    ) -> list[GroupInfo]:
        """Every page of one Cloud Identity membership search, as groups keyed by directory id."""
        url = f"{self.CLOUD_IDENTITY_BASE}/groups/-/memberships:{method}"
        params = {"query": query}
        groups: list[GroupInfo] = []
        for _page in range(_MAX_PAGES):
            response = await client.get(url, headers=headers, params=params)
            body = response.json()
            if not isinstance(body, dict):
                return groups
            relations = body.get("memberships") or []
            groups.extend(group for item in relations if (group := _group_from_relation(item)))
            page_token = body.get("nextPageToken")
            if not page_token:
                return groups
            params = {"pageToken": str(page_token), "query": query}
        logger.warning(
            "Google Cloud Identity: group search stopped at the page limit",
            method=method,
            pages=_MAX_PAGES,
            provider=self._provider.issuer,
        )
        return groups

    def _log_refusal(self, *, exc: httpx.HTTPStatusError, method: str) -> None:
        """Log a refused Cloud Identity call with Google's error code."""
        if exc.response.status_code == 403:
            message = (
                "Google Cloud Identity refused the user-token group lookup (403): the "
                "Cloud Identity API is disabled in the OAuth client's project, the login "
                "did not grant cloud-identity.groups.readonly, or the organisation "
                "blocks it -- the code says which"
            )
        else:
            message = "Google Cloud Identity refused the user-token group lookup"
        logger.warning(
            message,
            operation=method,
            provider=self._provider.issuer,
            **describe_idp_error(exc),
        )

    def _log_failure(self, *, exc: httpx.HTTPError | ValueError, method: str) -> None:
        """Log a Cloud Identity call that got no usable answer: no response, or a body that is not JSON."""
        logger.warning(
            "Google Cloud Identity group lookup failed",
            operation=method,
            provider=self._provider.issuer,
            **describe_idp_error(exc),
        )

    # ------------------------------------------------------------------
    # Directory API, as the service account
    # ------------------------------------------------------------------

    async def _groups_with_service_account(self, *, user_key: str) -> list[GroupInfo]:
        """The user's groups from the Directory API, or [] when no service account answers."""
        service = self._get_service()
        if service is None:
            return []

        try:
            raw = await asyncio.to_thread(self._fetch_user_groups_sync, service, user_key)
        except Exception as exc:
            logger.warning(
                "Google Admin SDK resolve_user_groups failed -- default deny",
                operation="resolve_user_groups",
                provider=self._provider.issuer,
                **describe_idp_error(exc),
            )
            return []

        return [GroupInfo(id=g["id"], name=g["name"], email=g.get("email", "")) for g in raw]

    def _service_account_json(self) -> str:
        """The service account JSON: the secret store first, then the env var."""
        return resolve_credential(
            secret_path=self._provider.groups.service_account_json_path,
            env_name=self._provider.groups.service_account_json_env,
            secrets=self._secrets,
        )

    def _get_service(self) -> Any | None:
        """Build a Directory API client that acts as the service account itself.

        Returns:
            A googleapiclient Resource object, or None if no service account is
            configured or its JSON does not build credentials.
        """
        raw_json = self._service_account_json()
        if not raw_json:
            return None

        try:
            sa_info = json.loads(raw_json)
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning(
                "Could not parse service account JSON",
                provider=self._provider.issuer,
                error=str(exc),
            )
            return None

        if not isinstance(sa_info, dict):
            logger.warning(
                "Service account JSON must be an object, got unexpected type",
                provider=self._provider.issuer,
                type=type(sa_info).__name__,
            )
            return None

        try:
            # Lazy import -- google packages may not be installed in all deployments
            from google.oauth2 import service_account  # type: ignore[import-untyped]
            from googleapiclient.discovery import build  # type: ignore[import-untyped]

            creds = service_account.Credentials.from_service_account_info(
                sa_info,
                scopes=[_DIRECTORY_SCOPE],
            )
            return build("admin", "directory_v1", credentials=creds, cache_discovery=False)

        except Exception as exc:
            logger.warning(
                "Failed to build Google Admin SDK service",
                provider=self._provider.issuer,
                error=str(exc),
            )
            return None

    # ------------------------------------------------------------------
    # Synchronous helpers (called via asyncio.to_thread)
    # ------------------------------------------------------------------

    def _fetch_all_groups_sync(self, service: Any) -> list[dict[str, Any]]:
        """Fetch all groups for the configured domain, handling pagination.

        Args:
            service: Google Admin SDK service resource.

        Returns:
            List of raw group dicts from the API response.
        """
        groups: list[dict[str, Any]] = []
        domain = self._provider.groups.domain
        page_token: str | None = None

        while True:
            kwargs: dict[str, Any] = {"maxResults": 200}
            if domain:
                kwargs["domain"] = domain
            if page_token:
                kwargs["pageToken"] = page_token

            response = service.groups().list(**kwargs).execute()
            groups.extend(response.get("groups", []))

            page_token = response.get("nextPageToken")
            if not page_token:
                break

        return groups

    def _fetch_user_groups_sync(self, service: Any, user_key: str) -> list[dict[str, Any]]:
        """Fetch the groups a single user belongs to, handling pagination.

        Uses ``groups().list(userKey=...)``. ``userKey`` and ``domain`` are
        mutually exclusive in the Directory API, so domain is deliberately NOT
        passed here.

        Args:
            service: Google Admin SDK service resource.
            user_key: The user's primary email or immutable id.

        Returns:
            List of raw group dicts the user is a member of.
        """
        groups: list[dict[str, Any]] = []
        page_token: str | None = None

        while True:
            kwargs: dict[str, Any] = {"userKey": user_key, "maxResults": 200}
            if page_token:
                kwargs["pageToken"] = page_token

            response = service.groups().list(**kwargs).execute()
            groups.extend(response.get("groups", []))

            page_token = response.get("nextPageToken")
            if not page_token:
                break

        return groups

    def _probe_sync(self, service: Any) -> None:
        """Perform a minimal API call to verify connectivity.

        Args:
            service: Google Admin SDK service resource.

        Raises:
            Exception: Any error from the Admin SDK (auth failure, network, etc.)
        """
        domain = self._provider.groups.domain
        kwargs: dict[str, Any] = {"maxResults": 1}
        if domain:
            kwargs["domain"] = domain
        service.groups().list(**kwargs).execute()
