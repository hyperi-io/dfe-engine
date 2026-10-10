#  Project:      dfe-engine
#  File:         tests/support/google_delegation.py
#  Purpose:      Mint a Google user access token through domain-wide delegation
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A Google user's access token, minted by a service account with domain-wide delegation.

Google blocks automated browser sign-in, so a live test cannot log a user in.
Instead the service account signs a JWT whose ``sub`` is the user and the token
endpoint swaps it for an access token that acts as that user, limited to the
scopes the Workspace admin delegated to the service account's client id.

No message raised here carries the key, the user's email or a token.
"""

import re
import time
from collections.abc import Mapping
from typing import Any

import httpx
import jwt
from scalo.http import AsyncHttpClient

GROUPS_READONLY_SCOPE = "https://www.googleapis.com/auth/cloud-identity.groups.readonly"

# Google refuses an assertion that outlives an hour, and a clock running ahead of Google's overshoots that unless the life is short.
ASSERTION_LIFETIME_SECONDS = 300

_JWT_BEARER_GRANT = "urn:ietf:params:oauth:grant-type:jwt-bearer"
_REQUIRED_KEYS = ("client_email", "private_key", "private_key_id", "token_uri")
_TIMEOUT_SECONDS = 10.0
_ERROR_CODE = re.compile(r"^[a-z_]{1,64}$")


class DelegationError(RuntimeError):
    """The delegated token could not be minted."""


def build_assertion(service_account: Mapping[str, Any], *, subject: str, scope: str) -> str:
    """Sign a JWT that asks the token endpoint for *scope* as *subject*.

    Args:
        service_account: The parsed service account key file.
        subject: Email of the Workspace user to act as.
        scope: The delegated scope to request.

    Returns:
        The signed assertion.

    Raises:
        DelegationError: The key file lacks a field, or its key cannot sign.
    """
    missing = [key for key in _REQUIRED_KEYS if not service_account.get(key)]
    if missing:
        raise DelegationError(f"service account JSON lacks: {', '.join(missing)}")
    issued_at = int(time.time())
    claims = {
        "aud": service_account["token_uri"],
        "exp": issued_at + ASSERTION_LIFETIME_SECONDS,
        "iat": issued_at,
        "iss": service_account["client_email"],
        "scope": scope,
        "sub": subject,
    }
    try:
        return jwt.encode(
            claims,
            service_account["private_key"],
            algorithm="RS256",
            headers={"kid": service_account["private_key_id"]},
        )
    except (TypeError, ValueError, jwt.PyJWTError) as exc:
        raise DelegationError("the service account private key could not sign") from exc


def _refusal_code(response: httpx.Response) -> str:
    """The OAuth ``error`` code of a refused exchange, or ``unspecified``."""
    try:
        body = response.json()
    except ValueError:
        return "unspecified"
    code = body.get("error") if isinstance(body, dict) else None
    if isinstance(code, str) and _ERROR_CODE.match(code):
        return code
    return "unspecified"


async def mint_user_access_token(
    service_account: Mapping[str, Any],
    *,
    subject: str,
    scope: str = GROUPS_READONLY_SCOPE,
) -> str:
    """Exchange a signed assertion for an access token that acts as *subject*.

    Args:
        service_account: The parsed service account key file.
        subject: Email of the Workspace user to act as.
        scope: The delegated scope to request.

    Returns:
        The user's access token.

    Raises:
        DelegationError: The key file is incomplete, the endpoint refused or
            could not be reached, or its answer holds no access token.
    """
    assertion = build_assertion(service_account, subject=subject, scope=scope)
    form = {"grant_type": _JWT_BEARER_GRANT, "assertion": assertion}
    try:
        async with AsyncHttpClient(timeout=_TIMEOUT_SECONDS, retries=1) as client:
            response = await client.post(service_account["token_uri"], data=form)
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        code = _refusal_code(exc.response)
        raise DelegationError(
            f"token endpoint refused the exchange: HTTP {status}, {code}"
        ) from None
    except httpx.HTTPError as exc:
        raise DelegationError(f"token endpoint unreachable: {type(exc).__name__}") from None
    try:
        token = response.json()["access_token"]
    except KeyError, TypeError, ValueError:
        token = None
    if not isinstance(token, str) or not token:
        raise DelegationError("token endpoint answered without an access token")
    return token
