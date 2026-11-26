#  Project:      dfe-engine
#  File:         auth/reauth.py
#  Purpose:      The grant a caller earns by proving it is still itself, which every access change needs
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The grant a caller earns by proving it is still itself, which every access change needs."""

from datetime import timedelta
from typing import Literal

from jwt import InvalidTokenError

from dfe_engine.auth.jwt_authority import JwtAuthority

REAUTH_AUDIENCE = "dfe-engine-account-reauth"
REAUTH_HEADER = "X-DFE-Reauth"
REAUTH_LIFETIME = timedelta(minutes=10)

type ReauthKind = Literal[
    "account", "api_key", "credential", "group", "org", "provider", "role", "setting"
]
REAUTH_KINDS: tuple[ReauthKind, ...] = (
    "account",
    "api_key",
    "credential",
    "group",
    "org",
    "provider",
    "role",
    "setting",
)

# Store names hold no colon, so a prefixed target never names an account or another kind.
_TARGET_PREFIXES: dict[str, str] = {
    "account": "",
    "api_key": "api-key:",
    "credential": "credential:",
    "group": "group:",
    "org": "org:",
    "provider": "provider:",
    "role": "role:",
    "setting": "setting:",
}


def grant_target(*, kind: ReauthKind, name: str) -> str:
    """The grant target for changing the *kind* called *name*."""
    return f"{_TARGET_PREFIXES[kind]}{name}"


def grant_allows(*, authority: JwtAuthority, caller: str, target: str, token: str) -> bool:
    """Whether *token* is a live grant this engine signed for *caller* to change *target*."""
    try:
        claims = authority.verify_for(audience=REAUTH_AUDIENCE, token=token)
    except InvalidTokenError:
        return False
    return (claims.get("sub") == caller) and (claims.get("target") == target)


def mint_reauth_grant(*, authority: JwtAuthority, caller: str, target: str) -> str:
    """Sign a grant letting *caller* change *target* for ``REAUTH_LIFETIME``."""
    claims = {"aud": REAUTH_AUDIENCE, "sub": caller, "target": target}
    return authority.sign(claims=claims, expires_delta=REAUTH_LIFETIME)
