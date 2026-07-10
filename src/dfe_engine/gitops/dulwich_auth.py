#  Project:      dfe-engine
#  File:         gitops/dulwich_auth.py
#  Purpose:      Shared dulwich HTTPS credential embed + on-disk scrub (F-GITOPS-TOKEN)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""dulwich HTTPS-credential helpers - embed for a single op, scrub from disk.

``dulwich.porcelain`` clone/push/fetch take no username/password kwargs; HTTPS
auth is carried IN THE URL. That has one security hazard: ``porcelain.clone``
persists whatever URL it cloned from into the clone's ``.git/config`` - so a
``https://user:token@host`` URL leaves the plaintext token on disk (a shared /
in-cluster volume, readable by any co-located sidecar, exec shell, or snapshot).

The safe pattern (F-GITOPS-TOKEN): supply the credentialed URL EXPLICITLY on each
op, and immediately scrub the stored remote back to the bare URL. These two
helpers are that pattern, shared by every dulwich caller (the gitops deploy repo +
the sigma git-repo provider) so the escaping and the scrub are written once.
"""

from __future__ import annotations

from pathlib import Path


def authed_https_url(
    url: str, username: str | None, token: str | None, *, default_username: str = "x-access-token"
) -> str:
    """Return ``url`` with basic-auth creds spliced in - HTTPS + token only.

    A non-HTTP URL (ssh / local path) or an absent token is returned UNCHANGED
    (SSH auths via the agent; a public repo needs nothing). When a token is
    present the username defaults to ``default_username`` (the git-forge
    token-user convention, e.g. GitHub's ``x-access-token``). The result is passed
    per-op and never persisted - pair every clone with :func:`scrub_remote_credentials`.
    """
    if not token or not url.startswith(("http://", "https://")):
        return url
    user = username or default_username
    scheme, rest = url.split("://", 1)
    return f"{scheme}://{user}:{token}@{rest}"


def scrub_remote_credentials(repo_path: str | Path, bare_url: str) -> None:
    """Rewrite ``remote.origin.url`` back to the credential-free ``bare_url``.

    Undoes the token that ``porcelain.clone`` baked into ``.git/config`` from a
    credentialed clone URL. push()/fetch() re-supply the authed URL explicitly
    every call, so the stored remote never needs the credential (F-GITOPS-TOKEN).
    Raises if the repo/config cannot be written - a caller that wants best-effort
    hardening wraps the call.
    """
    from dulwich.repo import Repo

    with Repo(str(repo_path)) as repo:
        config = repo.get_config()
        config.set((b"remote", b"origin"), b"url", bare_url.encode())
        config.write_to_path()
