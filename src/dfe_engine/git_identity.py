#  Project:      dfe-engine
#  File:         git_identity.py
#  Purpose:      Build the git author identity a governed write records
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Git author identity for governed writes.

A git identity must be ``Name <email>`` (dulwich rejects a bare name), so an
account with no email gets a deterministic placeholder ``name <name@<domain>>``.
"""

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from dfe_engine.auth.models import AuthContext

DEFAULT_FALLBACK_DOMAIN = "dfe.local"

_INVALID_LOCAL_PART = re.compile(r"[^a-z0-9._-]")


def _sanitise_local_part(user_id: str) -> str:
    """Reduce a user id to a safe email local-part for placeholder identities."""
    local = _INVALID_LOCAL_PART.sub("-", user_id.lower()).strip("-.")
    return local or "user"


def git_author(user: AuthContext, *, fallback_domain: str = DEFAULT_FALLBACK_DOMAIN) -> str:
    """Build a ``Name <email>`` git author identity from an AuthContext.

    Uses the user's real email when present (OIDC/JWT). For identities with no
    email (local accounts, API keys, dev mode), synthesizes a deterministic
    placeholder so the commit still attributes to the account name.

    Args:
        fallback_domain: Email domain for synthesized placeholder identities.
        user: The authenticated identity.

    Returns:
        A git author string such as ``alice <alice@corp.com>`` or, for an
        emailless account, ``admin <admin@dfe.local>``.
    """
    if user.email:
        return f"{user.user_id} <{user.email}>"
    local = _sanitise_local_part(user.user_id)
    return f"{user.user_id} <{local}@{fallback_domain}>"
