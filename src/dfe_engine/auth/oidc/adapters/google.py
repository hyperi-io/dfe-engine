#  Project:      DFE Engine
#  File:         src/dfe_engine/auth/oidc/adapters/google.py
#  Purpose:      Google Workspace Admin SDK adapter for OIDC group resolution.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

import asyncio
import json
from typing import Any

from scalo.logger import logger

from dfe_engine.auth.oidc.adapters.base import OIDCGroupAdapter
from dfe_engine.auth.oidc.credential_env import resolve_credential
from dfe_engine.auth.oidc.models import GroupInfo

# Google Admin SDK scope for read-only group directory access.
_DIRECTORY_SCOPE = "https://www.googleapis.com/auth/admin.directory.group.readonly"


class GoogleAdapter(OIDCGroupAdapter):
    """Resolves Google Workspace group IDs/emails to display names via Admin SDK.

    Requires a service account with domain-wide delegation configured for the
    admin.directory.group.readonly scope. The service account JSON resolves from
    provider.groups.service_account_json_path in the DfeSecrets seam, falling
    back to the env var named in service_account_json_env.

    Falls back gracefully when credentials are missing or the API is unavailable —
    all public methods return safe empty/identity values rather than raising.
    """

    # ------------------------------------------------------------------
    # Public async interface
    # ------------------------------------------------------------------

    async def resolve_groups(self, group_ids: list[str]) -> dict[str, str]:
        """Resolve Google group IDs or emails to display names via Admin SDK.

        Args:
            group_ids: Group email addresses or unique IDs from OIDC token claims.

        Returns:
            Mapping of {group_id: display_name}. Falls back to {id: id} for any
            group that could not be resolved, and for all groups when credentials
            are missing or the API call fails.
        """
        if not group_ids:
            return {}

        service = self._get_service()
        if service is None:
            # No credentials — return identity map so callers still have usable keys
            return {g: g for g in group_ids}

        try:
            all_groups = await asyncio.to_thread(self._fetch_all_groups_sync, service)
        except Exception as exc:
            logger.warning(
                "Google Admin SDK group fetch failed — using identity fallback",
                provider=self._provider.issuer,
                error=str(exc),
            )
            return {g: g for g in group_ids}

        # Build a lookup by both email and id
        lookup: dict[str, str] = {}
        for group in all_groups:
            lookup[group["id"]] = group["name"]
            if group.get("email"):
                lookup[group["email"]] = group["name"]

        return {g: lookup.get(g, g) for g in group_ids}

    async def resolve_user_groups(self, directory_id: str) -> list[GroupInfo]:
        """Return the groups a user belongs to via the Directory API.

        Google never puts group membership in the id_token, so this login-time
        enrichment is the ONLY way to know a Google user's groups. Calls
        ``groups().list(userKey=...)`` - ``userKey`` accepts the user's primary
        email or their immutable id, so the RP can pass either.

        Requires the same read-only domain-wide-delegation service account as the
        other methods (``admin.directory.group.readonly``). Fails open: missing
        credentials or an API error returns ``[]`` (default deny), never raises.
        """
        if not directory_id:
            return []

        service = self._get_service()
        if service is None:
            return []

        try:
            raw = await asyncio.to_thread(self._fetch_user_groups_sync, service, directory_id)
        except Exception as exc:
            logger.warning(
                "Google Admin SDK resolve_user_groups failed — default deny",
                provider=self._provider.issuer,
                error=str(exc),
            )
            return []

        return [GroupInfo(id=g["id"], name=g["name"], email=g.get("email", "")) for g in raw]

    async def list_all_groups(self) -> list[GroupInfo]:
        """List all groups in the configured Google Workspace domain.

        Uses the Admin SDK Directory API groups.list with domain filtering and
        handles pagination transparently.

        Returns:
            List of GroupInfo. Returns empty list when credentials are missing
            or the API call fails.
        """
        service = self._get_service()
        if service is None:
            return []

        try:
            raw = await asyncio.to_thread(self._fetch_all_groups_sync, service)
        except Exception as exc:
            logger.warning(
                "Google Admin SDK list_all_groups failed",
                provider=self._provider.issuer,
                error=str(exc),
            )
            return []

        return [
            GroupInfo(
                id=g["id"],
                name=g["name"],
                email=g.get("email", ""),
            )
            for g in raw
        ]

    async def test_connection(self) -> tuple[bool, str]:
        """Test connectivity to the Google Admin SDK.

        Performs a minimal groups.list call (maxResults=1) to verify that
        credentials are valid and domain-wide delegation is working.

        Returns:
            (True, "OK") on success, or (False, error_message) on failure.
        """
        service = self._get_service()
        if service is None:
            if not self._service_account_json():
                return False, (
                    "Service account not configured (send it as "
                    "'groups.service_account_json' to the provider API, or set "
                    "groups.service_account_json_env)"
                )
            return False, "Failed to build service from the configured service account JSON"

        try:
            await asyncio.to_thread(self._probe_sync, service)
            return True, "OK"
        except Exception as exc:
            logger.warning(
                "Google Admin SDK test_connection failed",
                provider=self._provider.issuer,
                error=str(exc),
            )
            return False, str(exc)

    # ------------------------------------------------------------------
    # Service construction
    # ------------------------------------------------------------------

    def _service_account_json(self) -> str:
        """The service account JSON: the secret store first, then the env var."""
        return resolve_credential(
            secret_path=self._provider.groups.service_account_json_path,
            env_name=self._provider.groups.service_account_json_env,
            secrets=self._secrets,
        )

    def _get_service(self) -> Any | None:
        """Build a Google Admin SDK service resource from service account credentials.

        Applies domain-wide delegation via with_subject() when
        provider.groups.admin_email is configured.

        Returns:
            A googleapiclient Resource object, or None if credentials are
            unavailable or invalid.
        """
        raw_json = self._service_account_json()
        if not raw_json:
            return None

        # Parse the service account JSON — bad JSON or wrong type returns None
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
            # Lazy import — google packages may not be installed in all deployments
            from google.oauth2 import service_account  # type: ignore[import-untyped]
            from googleapiclient.discovery import build  # type: ignore[import-untyped]

            creds = service_account.Credentials.from_service_account_info(
                sa_info,
                scopes=[_DIRECTORY_SCOPE],
            )

            admin_email = self._provider.groups.admin_email
            if admin_email:
                creds = creds.with_subject(admin_email)

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
