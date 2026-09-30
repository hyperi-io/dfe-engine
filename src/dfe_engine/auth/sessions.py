#  Project:      dfe-engine
#  File:         auth/sessions.py
#  Purpose:      The session claims an engine token carries, and when a session has ended
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Session state an engine token carries, checked against its account on every request.

A token is a bearer credential the client holds, so ending a session happens on the
account: every token carries its account's session marker
(:meth:`~dfe_engine.auth.accounts.Account.session_marker`) from when it was minted,
and one whose marker the account no longer has is refused. The marker moves on a
logout, a password change, the account being disabled, blocked, re-enabled or
unblocked, and the account being deleted and created again under its name.

Every token also carries ``auth_time``, when its owner last signed in. A refresh
carries it forward and is refused once the session is older than the configured
maximum, so a token cannot be renewed indefinitely.
"""

from collections.abc import Mapping
from typing import Any

from dfe_engine.auth.accounts import Account

SESSION_CLAIM = "session_epoch"
"""The account's session marker when the token was minted."""

AUTH_TIME_CLAIM = "auth_time"
"""When the token's owner last signed in, as epoch seconds (the OIDC claim name)."""


def session_claims(account: Account | None, *, auth_time: int) -> dict[str, object]:
    """The session claims a token minted now for *account* carries.

    Args:
        account: The account the token is for. None names no account, and a
            token carrying the empty marker that results is refused on use.
        auth_time: When the owner signed in, as epoch seconds.

    Returns:
        The claims to merge into the token.
    """
    marker = account.session_marker() if account is not None else ""
    return {SESSION_CLAIM: marker, AUTH_TIME_CLAIM: auth_time}


def session_ended(account: Account | None, claims: Mapping[str, Any]) -> bool:
    """Whether the session a verified token belongs to has ended.

    A token minted before tokens carried a marker is accepted only while its
    account has never ended its sessions, which bounds it by its own expiry.

    Args:
        account: The account the token's subject binds, or None.
        claims: The verified token's claims.

    Returns:
        True when the token must be refused.
    """
    claimed = claims.get(SESSION_CLAIM)
    if account is None:
        return claimed is not None
    if claimed is None:
        return bool(account.session_epoch)
    return claimed != account.session_marker()


def session_auth_time(claims: Mapping[str, Any] | None, *, now: int) -> int:
    """When the session behind *claims* began, as epoch seconds.

    A token minted before it carried ``auth_time`` began no later than it was
    issued. A session with no token behind it, one a trusted proxy authenticates
    on every request, began now.
    """
    if not claims:
        return now
    value = claims.get(AUTH_TIME_CLAIM, claims.get("iat"))
    return int(value) if isinstance(value, int | float) else now


def remaining_seconds(auth_time: int, *, now: int, max_session_minutes: int) -> int:
    """Seconds a session that began at *auth_time* may still run; zero or less once over."""
    return auth_time + max_session_minutes * 60 - now


def token_lifetime(
    auth_time: int, *, now: int, expire_minutes: int, max_session_minutes: int
) -> int:
    """Seconds a token minted now lives: its usual lifetime, cut short at the session's end."""
    remaining = remaining_seconds(auth_time, now=now, max_session_minutes=max_session_minutes)
    return min(expire_minutes * 60, remaining)
