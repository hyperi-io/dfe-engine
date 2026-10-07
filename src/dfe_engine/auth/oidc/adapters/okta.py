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
from urllib.parse import urlparse, urlsplit

from pydantic import ValidationError
from scalo.logger import logger

from dfe_engine.auth.oidc.adapters.base import (
    DirectoryError,
    OIDCGroupAdapter,
    describe_error,
    error_frames,
    fetch_directory_page,
)
from dfe_engine.auth.oidc.credential_env import resolve_credential
from dfe_engine.auth.oidc.models import GroupInfo


class OktaAdapter(OIDCGroupAdapter):
    """Okta Groups API adapter."""

    async def resolve_groups(self, group_ids: list[str]) -> dict[str, str]:
        """Resolve Okta group IDs to display names."""
        if not group_ids:
            return {}

        base, headers = self._api_base_and_headers()
        if base is None or headers is None:
            return {gid: gid for gid in group_ids}

        from scalo.http import AsyncHttpClient

        result: dict[str, str] = {}
        async with AsyncHttpClient() as client:
            for gid in group_ids:
                url = f"{base}/groups/{gid}"
                try:
                    response = await client.get(url, headers=headers)
                    data = response.json()
                    profile = data.get("profile") or {}
                    name = profile.get("name") or profile.get("description") or gid
                    result[gid] = name
                except Exception as exc:
                    logger.warning(
                        "Okta resolve_groups: failed to resolve group",
                        group_id=gid,
                        error=describe_error(exc=exc, read_summary=_okta_error_summary),
                    )
                    result[gid] = gid
        return result

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
                error=describe_error(exc=exc, read_summary=_okta_error_summary),
            )
            return []

    async def list_all_groups(self) -> list[GroupInfo]:
        """List every Okta group, following the ``Link`` header's next page.

        Raises :class:`DirectoryError` when the domain or API token is not configured or any page fails, so no partial listing is returned.
        """
        provider_type = self._provider.type
        base, headers = self._api_base_and_headers()
        if base is None:
            raise DirectoryError(
                detail="'groups.okta_domain' is not set", provider_type=provider_type
            )
        if headers is None:
            env = self._provider.groups.api_token_env
            where = f"the env var {env!r}" if env else "an env var named in 'groups.api_token_env'"
            detail = f"no API token; send 'groups.api_token' to the provider API or set {where}"
            if self._api_token():
                detail = f"the API token {self._unsendable_token()}"
            raise DirectoryError(detail=detail, provider_type=provider_type)

        from scalo.http import AsyncHttpClient

        groups = []
        url = f"{base}/groups?limit=200"
        async with AsyncHttpClient() as client:
            while url:
                body, response_headers = await fetch_directory_page(
                    client=client,
                    headers=headers,
                    provider_type=provider_type,
                    read_summary=_okta_error_summary,
                    url=url,
                )
                groups.extend(_okta_page_groups(body=body, provider_type=provider_type, url=url))
                url = _next_url_from_link_header(response_headers.get("link"))
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
            if self._api_token():
                return False, f"Okta API token {self._unsendable_token()}"
            env = self._provider.groups.api_token_env or "groups.api_token_env"
            return False, (
                "Okta API token not configured (send it as 'groups.api_token' to the "
                f"provider API, or set env var '{env}')"
            )

        from scalo.http import AsyncHttpClient

        provider_type = self._provider.type
        url = f"{base}/groups?limit=1"
        try:
            async with AsyncHttpClient() as client:
                body, _response_headers = await fetch_directory_page(
                    client=client,
                    headers=headers,
                    provider_type=provider_type,
                    read_summary=_okta_error_summary,
                    url=url,
                )
            groups = _okta_page_groups(body=body, provider_type=provider_type, url=url)
        except DirectoryError as exc:
            logger.warning(
                "Okta test_connection failed", error=exc.detail, provider=self._provider.issuer
            )
            return False, f"Okta Groups API connection failed: {exc.detail}"
        except Exception as exc:
            # An unexpected error's text could quote the request's credential, so only its type is reported.
            logger.error(
                "Okta test_connection failed unexpectedly",
                error_type=type(exc).__name__,
                frames=error_frames(exc=exc),
                provider=self._provider.issuer,
            )
            return False, f"Okta Groups API connection failed: {type(exc).__name__}"
        return True, f"Okta Groups API connection successful -- {len(groups)} group(s) returned"

    def _api_base_and_headers(self) -> tuple[str | None, dict[str, str] | None]:
        base = _normalize_okta_api_base(self._provider.groups.okta_domain)
        if not base:
            return None, None

        token = self._api_token()
        if not (_HEADER_SAFE_TOKEN.fullmatch(token)):
            return base, None
        return base, {"Authorization": f"SSWS {token}", "Accept": "application/json"}

    def _api_token(self) -> str:
        """The configured API token: the secret store first, then the env var."""
        return resolve_credential(
            env_name=self._provider.groups.api_token_env,
            secret_path=self._provider.groups.api_token_path,
            secrets=self._secrets,
        )

    def _unsendable_token(self) -> str:
        """Why the configured API token cannot be sent and where to fix it, without its value."""
        env = self._provider.groups.api_token_env
        where = f"the env var {env!r}" if env else "the env var named in 'groups.api_token_env'"
        return (
            "holds a character an HTTP header cannot carry: a space, a control character or a "
            f"non-ASCII one; re-send 'groups.api_token' to the provider API or fix {where}"
        )


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

# Printable ASCII without spaces: a token holding anything else is never put in a header, since the HTTP client's error quotes the header it refuses and its retry hook logs that error.
_HEADER_SAFE_TOKEN = re.compile(r"[\x21-\x7e]+")


def _next_url_from_link_header(link: str | None) -> str | None:
    if not link:
        return None
    match = _LINK_NEXT_RE.search(link)
    return match.group(1) if match else None


def _okta_error_summary(body: Any) -> str:
    """The ``errorSummary`` Okta puts in an error response; empty when there is none."""
    summary = body.get("errorSummary") if isinstance(body, dict) else None
    return summary if isinstance(summary, str) else ""


def _okta_page_groups(*, body: Any, provider_type: str, url: str) -> list[GroupInfo]:
    """The groups on one page of an Okta listing.

    Raises :class:`DirectoryError` when the body is not a list of group objects.
    """
    detail = f"GET {urlsplit(url).path!r} returned a body that is not a list of groups"
    if not (isinstance(body, list)):
        raise DirectoryError(detail=detail, provider_type=provider_type)
    try:
        return [_group_info_from_okta(item) for item in body]
    except (AttributeError, ValidationError) as exc:
        # A group or profile that is not an object has no .get; a non-string name fails validation.
        raise DirectoryError(detail=detail, provider_type=provider_type) from exc
