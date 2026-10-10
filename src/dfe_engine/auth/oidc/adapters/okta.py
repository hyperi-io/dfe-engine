#  Project:      dfe-engine
#  File:         auth/oidc/adapters/okta.py
#  Purpose:      Okta Groups API adapter for OIDC group resolution
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Okta OIDC group adapter.

Most deployments use ``token_claim`` mode (groups in the ID token via Okta
claim mapping) and never call the Groups API. When ``groups.mode == "api"``,
this adapter lists and resolves groups using the Okta Management API
(``SSWS`` token + ``okta_domain``).
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from scalo.logger import logger

from dfe_engine.auth.oidc.adapters.base import OIDCGroupAdapter
from dfe_engine.auth.oidc.credential_env import resolve_credential
from dfe_engine.auth.oidc.models import GroupInfo


class OktaAdapter(OIDCGroupAdapter):
    """Okta Groups API adapter."""

    async def resolve_user_groups(self, directory_id: str) -> list[GroupInfo]:
        """List groups for a user (Okta user id or login)."""
        if not directory_id:
            return []

        base, headers = self._api_base_and_headers()
        if base is None or headers is None:
            return []

        from scalo.http import AsyncHttpClient

        url: str | None = f"{base}/users/{directory_id}/groups?limit=200"
        try:
            async with AsyncHttpClient() as client:
                groups: list[GroupInfo] = []
                while url:
                    response = await client.get(url, headers=headers)
                    data = response.json()
                    if isinstance(data, list):
                        for item in data:
                            groups.append(_group_info_from_okta(item))
                    url = _next_url_from_link_header(getattr(response, "headers", {}).get("link"))
                return groups
        except Exception as exc:
            logger.warning(
                "Okta resolve_user_groups failed -- default deny",
                provider=self._provider.issuer,
                error=str(exc),
            )
            return []

    async def list_all_groups(self) -> list[GroupInfo]:
        """List all Okta groups (paginated)."""
        base, headers = self._api_base_and_headers()
        if base is None or headers is None:
            return []

        from scalo.http import AsyncHttpClient

        groups: list[GroupInfo] = []
        url: str | None = f"{base}/groups?limit=200"

        async with AsyncHttpClient() as client:
            while url:
                try:
                    response = await client.get(url, headers=headers)
                    data = response.json()
                except Exception as exc:
                    logger.warning(
                        "Okta list_all_groups: API call failed",
                        url=url,
                        error=str(exc),
                    )
                    break

                if isinstance(data, list):
                    for item in data:
                        groups.append(_group_info_from_okta(item))
                url = _next_url_from_link_header(getattr(response, "headers", {}).get("link"))

        return groups

    async def test_connection(self) -> tuple[bool, str]:
        """Test directory connectivity (API mode) or report N/A for token_claim."""
        mode = self._provider.groups.mode
        if mode != "api":
            return (
                True,
                f"Okta {mode} mode -- no directory API to test "
                "(use GET .../verify-login for OIDC login configuration)",
            )

        base, headers = self._api_base_and_headers()
        if base is None:
            return False, "Okta domain not configured (groups.okta_domain)"
        if headers is None:
            env = self._provider.groups.api_token_env or "groups.api_token_env"
            return False, (
                "Okta API token not configured (send it as 'groups.api_token' to the "
                f"provider API, or set env var '{env}')"
            )

        from scalo.http import AsyncHttpClient

        url = f"{base}/groups?limit=1"
        try:
            async with AsyncHttpClient() as client:
                response = await client.get(url, headers=headers)
                data = response.json()
                count = len(data) if isinstance(data, list) else 0
                return True, f"Okta Groups API connection successful -- {count} group(s) returned"
        except Exception as exc:
            logger.warning(
                "Okta test_connection failed",
                provider=self._provider.issuer,
                error=str(exc),
            )
            return False, f"Okta Groups API connection failed: {exc}"

    def _api_base_and_headers(self) -> tuple[str | None, dict[str, str] | None]:
        base = _normalize_okta_api_base(self._provider.groups.okta_domain)
        if not base:
            return None, None

        token = resolve_credential(
            secret_path=self._provider.groups.api_token_path,
            env_name=self._provider.groups.api_token_env,
            secrets=self._secrets,
        )
        if not token:
            return base, None
        return base, {"Authorization": f"SSWS {token}", "Accept": "application/json"}


def _normalize_okta_api_base(okta_domain: str) -> str | None:
    domain = (okta_domain or "").strip()
    if not domain:
        return None
    if domain.startswith("http://") or domain.startswith("https://"):
        parsed = urlparse(domain)
        host = parsed.netloc or parsed.path.split("/")[0]
    else:
        host = domain.split("/")[0]
    if not host:
        return None
    return f"https://{host}/api/v1"


def _group_info_from_okta(item: dict[str, Any]) -> GroupInfo:
    profile = item.get("profile") or {}
    gid = item.get("id") or ""
    name = profile.get("name") or profile.get("description") or gid
    return GroupInfo(
        id=gid,
        name=name,
        email=profile.get("email") or "",
        description=profile.get("description") or "",
    )


_LINK_NEXT_RE = re.compile(r'<([^>]+)>\s*;\s*rel="next"')


def _next_url_from_link_header(link: str | None) -> str | None:
    if not link:
        return None
    match = _LINK_NEXT_RE.search(link)
    return match.group(1) if match else None
