#  Project:      dfe-engine
#  File:         git_identity.py
#  Purpose:      Build git author/committer identities and commit registry files
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Git identity helpers for registry writes.

A git commit records two identities: the ``author`` (who made the change)
and the ``committer`` (what applied it). For registry writes we set:

- author    = the requesting user, derived from their AuthContext. OIDC/JWT
  users carry a real email; local accounts, API keys, and dev mode have none,
  so we synthesize a deterministic placeholder ``name <name@<domain>>``.
- committer = a fixed service identity (the engine), so ``git log`` reads
  "alice authored, dfe-engine committed" - which is literally what happened.

A git author/committer string must be ``Name <email>`` (dulwich rejects a bare
name with no angle-bracketed email), so a bare ``user_id`` like ``admin`` is
never a valid identity on its own - hence the placeholder fallback.

Usage:
    from dfe_engine.git_identity import commit_file, git_author

    author = git_author(user)                       # at the API boundary
    commit_file(store, yaml_path, message, author=author)   # in the registry
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from dulwich import porcelain as git
from hyperi_pylib.logger import logger

if TYPE_CHECKING:
    from hyperi_pylib.config import DirectoryConfigStore

    from dfe_engine.auth.models import AuthContext

DEFAULT_FALLBACK_DOMAIN = "dfe.local"
COMMITTER_NAME = "dfe-engine"
COMMITTER_EMAIL = "noreply@dfe.local"
COMMITTER_IDENTITY = f"{COMMITTER_NAME} <{COMMITTER_EMAIL}>"

_INVALID_LOCAL_PART = re.compile(r"[^a-z0-9._-]")


def _sanitise_local_part(user_id: str) -> str:
    """Reduce a user id to a safe email local-part for placeholder identities."""
    local = _INVALID_LOCAL_PART.sub("-", user_id.lower()).strip("-.")
    return local or "user"


def commit_file(
    store: DirectoryConfigStore,
    file_path: Path,
    message: str,
    *,
    author: str | None,
    committer: str = COMMITTER_IDENTITY,
) -> None:
    """Stage and commit a single file with split author/committer identities.

    Mirrors ``DirectoryConfigStore._git_commit`` but records ``committer``
    separately from ``author`` (pylib collapses the two). A no-op when the
    store is not git-backed.

    Args:
        author: Git author identity (``Name <email>``); falls back to the
            committer service identity when None.
        committer: Git committer identity (``Name <email>``).
        file_path: Path to the file to stage and commit.
        message: Commit subject.
        store: The directory store backing the registry.
    """
    repo = store._repo
    if repo is None:
        return
    try:
        rel_path = str(file_path.resolve(strict=False).relative_to(Path(repo.path).resolve()))
        git.add(repo, paths=[rel_path])
        author_bytes = (author or committer).encode("utf-8")
        commit_id = git.commit(
            repo,
            author=author_bytes,
            committer=committer.encode("utf-8"),
            message=message.encode("utf-8"),
        )
        logger.debug(f"Git commit {commit_id.decode()[:8]}: {message}")
    except Exception as e:
        logger.error(f"Git commit failed: {e}")


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
