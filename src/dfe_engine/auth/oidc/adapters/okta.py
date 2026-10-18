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
(``SSWS`` token + ``okta_domain``). Either way a group links on its name, the
identifier Okta's groups claim carries.
"""

import functools
import re
from typing import Any
from urllib.parse import urlparse

from scalo.concurrency import run_blocking
from scalo.logger import logger

from dfe_engine.auth.oidc.adapters.base import (
    DirectoryCredentialError,
    DirectoryCredentialUnreadableError,
    DirectoryNotConfiguredError,
    DirectoryPageError,
    DirectoryPageLimitError,
    LookupFailure,
    OIDCGroupAdapter,
    lookup_failure,
    missing_credential,
    same_origin_link,
)
from dfe_engine.auth.oidc.credential_env import (
    ResolvedCredential,
    resolve_credential,
    unreadable_credential_message,
)
from dfe_engine.auth.oidc.models import GroupInfo
from dfe_engine.http_errors import describe_http_error, http_failure_message


class OktaAdapter(OIDCGroupAdapter):
    """Okta Groups API adapter."""

    async def resolve_user_groups(self, directory_id: str) -> list[GroupInfo] | LookupFailure:
        """List groups for a user (Okta user id or login).

        A call that got no answer, a 429 or a 5xx is an outage. A domain or token not
        configured, any other refused call, a page that is not a list of groups or a
        listing still paging at :attr:`MAX_LOGIN_PAGES` is a refusal. Either is logged,
        and never returns the groups read before it.
        """
        if not directory_id:
            return []

        base, headers = await run_blocking(self._api_base_and_headers)
        if base is None or headers is None:
            # A token that is set but never sent finds no groups, so the login says why once.
            if base is None:
                return LookupFailure.REFUSED
            if self._api_token.value:
                self._warn_unsendable_token(operation="resolve_user_groups")
            token = self._api_token
            return missing_credential(unreadable=token.unreadable, refused=token.refused)

        from scalo.http import AsyncHttpClient

        url: str | None = f"{base}/users/{directory_id}/groups?limit=200"
        try:
            async with AsyncHttpClient() as client:
                groups: list[GroupInfo] = []
                for _page in range(self.MAX_LOGIN_PAGES):
                    response = await client.get(url, headers=headers)
                    data = response.json()
                    if not isinstance(data, list):
                        raise DirectoryPageError
                    groups.extend(_group_info_from_okta(item) for item in data)
                    url = _next_url_from_link_header(getattr(response, "headers", {}).get("link"))
                    if not url:
                        return groups
                    url = same_origin_link(url, directory_url=base)
            raise DirectoryPageLimitError(self.MAX_LOGIN_PAGES)
        except Exception as exc:
            failure = lookup_failure(exc)
            logger.warning(
                "Okta resolve_user_groups failed",
                failure=failure,
                operation="resolve_user_groups",
                provider=self._provider.issuer,
                **describe_http_error(exc),
            )
            return failure

    async def list_all_groups(self) -> list[GroupInfo]:
        """List all Okta groups (paginated).

        Returns:
            Every group.

        Raises:
            DirectoryNotConfiguredError: The domain or the API token is not configured.
            DirectoryCredentialUnreadableError: The API token's secret store did
                not answer or refused.
            DirectoryCredentialError: The API token holds a character an HTTP header
                cannot carry, so it is never sent.
            DirectoryPageError: A page Okta served was not a list of groups.
            DirectoryPageLimitError: Okta still linked a next page at the page limit.
            DirectoryForeignLinkError: Okta linked the next page off its own origin.
            Exception: Whatever the client raised on a page Okta did not serve.
                A listing cut short is never returned: group sync would record
                the groups read so far as the whole directory.
        """
        base, headers = await run_blocking(self._api_base_and_headers)
        if base is None or headers is None:
            if base is not None and self._api_token.value:
                self._warn_unsendable_token(operation="list_all_groups")
                raise DirectoryCredentialError
            if base is not None and self._api_token.unreadable:
                raise DirectoryCredentialUnreadableError(refused=self._api_token.refused)
            raise DirectoryNotConfiguredError

        from scalo.http import AsyncHttpClient

        groups: list[GroupInfo] = []
        url: str | None = f"{base}/groups?limit=200"

        async with AsyncHttpClient() as client:
            for _page in range(self.MAX_LISTING_PAGES):
                response = await client.get(url, headers=headers)
                data = response.json()
                if not isinstance(data, list):
                    raise DirectoryPageError
                groups.extend(_group_info_from_okta(item) for item in data)
                url = _next_url_from_link_header(getattr(response, "headers", {}).get("link"))
                if not url:
                    return groups
                url = same_origin_link(url, directory_url=base)
        raise DirectoryPageLimitError(self.MAX_LISTING_PAGES)

    async def test_connection(self) -> tuple[bool, str]:
        """Test directory connectivity (API mode) or report N/A for token_claim."""
        mode = self._provider.groups.mode
        if mode != "api":
            return (
                True,
                f"Okta {mode} mode -- no directory API to test "
                "(use GET .../verify-login for OIDC login configuration)",
            )

        base, headers = await run_blocking(self._api_base_and_headers)
        if base is None:
            return False, "Okta domain not configured (groups.okta_domain)"
        if headers is None and self._api_token.value:
            return False, f"Okta API token {self._unsendable_token()}"
        if headers is None and self._api_token.unreadable:
            return False, unreadable_credential_message(
                credential="Okta API token", refused=self._api_token.refused
            )
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
                operation="test_connection",
                provider=self._provider.issuer,
                **describe_http_error(exc),
            )
            return False, http_failure_message(exc, service="Okta Groups API")

    @functools.cached_property
    def _api_token(self) -> ResolvedCredential:
        """The Management API token: the secret store first, then the env var.

        Resolved once per adapter, which serves one sync, one connection test or one
        login, so an unreachable store is asked and logged once. The first access reads
        the store, which blocks, so every caller reaches it through
        ``_api_base_and_headers`` under ``run_blocking`` before reading it on the loop.
        """
        return resolve_credential(
            secret_path=self._provider.groups.api_token_path,
            env_name=self._provider.groups.api_token_env,
            secrets=self._secrets,
        )

    def _api_base_and_headers(self) -> tuple[str | None, dict[str, str] | None]:
        """Resolve the Management API origin and its request headers.

        Blocking: it reads the API token from the secret store, so async callers run
        it with ``run_blocking``.
        """
        base = _normalize_okta_api_base(self._provider.groups.okta_domain)
        if not base:
            return None, None

        token = self._api_token.value
        if not (_HEADER_SAFE_TOKEN.fullmatch(token)):
            return base, None
        return base, {"Authorization": f"SSWS {token}", "Accept": "application/json"}

    def _unsendable_token(self) -> str:
        """Why the configured API token cannot be sent and where to fix it, without its value."""
        env = self._provider.groups.api_token_env
        where = f"the env var {env!r}" if env else "the env var named in 'groups.api_token_env'"
        return (
            "holds a character an HTTP header cannot carry: a space, a control character or a "
            f"non-ASCII one; re-send 'groups.api_token' to the provider API or fix {where}"
        )

    def _warn_unsendable_token(self, *, operation: str) -> None:
        """Log that the configured API token is never sent, naming the settings that hold it and never its value."""
        logger.warning(
            "Okta API token not sent: it holds a character an HTTP header cannot carry",
            env_var=self._provider.groups.api_token_env,
            store_path=self._provider.groups.api_token_path,
            operation=operation,
            provider=self._provider.issuer,
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
    """The GroupInfo for one Okta group, its id being the group's name.

    Okta's groups claim carries group names, never the ``00g...`` ids, so the
    name is what a sign-in and the group sync must both link on. A group with no
    name has nothing a sign-in could assert, so it carries no id.
    """
    profile = item.get("profile") or {}
    link_name = profile.get("name") or ""
    return GroupInfo(
        id=link_name,
        name=link_name or profile.get("description") or item.get("id") or "",
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
