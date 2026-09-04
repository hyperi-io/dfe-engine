#  Project:      dfe-engine
#  File:         gitops/dulwich_auth.py
#  Purpose:      Shared dulwich HTTPS credential embed + on-disk scrub (F-GITOPS-TOKEN)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""dulwich HTTPS-credential helpers - embed for a single op, keep it off disk and
out of the logs.

``dulwich.porcelain`` clone/push/fetch take no username/password kwargs; HTTPS
auth is carried IN THE URL. That has two security hazards:

- ``porcelain.clone`` persists whatever URL it cloned from into the clone's
  ``.git/config`` - so a ``https://user:token@host`` URL leaves the plaintext
  token on disk (a shared / in-cluster volume, readable by any co-located
  sidecar, exec shell, or snapshot);
- every porcelain op writes the URL AS SUPPLIED to its ``errstream`` (stderr by
  default) and into its failure messages - ``Push to <url> successful.`` puts the
  deploy token in the container log in clear.

The safe pattern (F-GITOPS-TOKEN): supply the credentialed URL EXPLICITLY on each
op, scrub the stored remote back to the bare URL, and route the op's output and
errors through the redactors here. These helpers are that pattern, shared by
every dulwich caller (the gitops deploy repo + the sigma git-repo provider) so
the escaping, the scrub and the redaction are written once.
"""

from __future__ import annotations

import io
import re
from pathlib import Path

from scalo.logger import logger

# ``scheme://user:secret@host`` - the userinfo half of a URL, anywhere in a string.
# Requires the colon, so a credential-free ``ssh://git@host`` is left alone.
_USERINFO_RE = re.compile(r"(?<=://)[^/@\s]+:[^/@\s]*@")


def redact_credentials(text: str) -> str:
    """Return *text* with the ``user:secret@`` userinfo of every URL replaced.

    Applied to anything derived from a credentialed remote URL before it reaches a
    log, a stream or an exception message - dulwich embeds the URL verbatim in all
    three.
    """
    return _USERINFO_RE.sub("***@", text)


class RedactingErrStream(io.RawIOBase):
    """Binary sink for a dulwich op's ``errstream``, redacted and logged at debug.

    ``porcelain.push`` writes ``Push to <remote_location> successful.`` here, and
    ``remote_location`` is the URL as supplied - including the basic-auth token the
    HTTPS path has to carry. The default errstream is stderr, so that line puts the
    deploy-repo token in the container log in clear.

    Redaction is per LINE, not per chunk: dulwich writes progress in whatever sizes
    the transport hands it, so a URL split across two writes would pass the regex
    unredacted if each chunk were matched on its own.
    """

    def __init__(self) -> None:
        self._buffer = bytearray()

    def writable(self) -> bool:
        """Always writable - this stream only ever accepts output."""
        return True

    def write(self, data, /) -> int:
        """Buffer the chunk, log every complete line redacted, report it consumed."""
        raw = bytes(data)
        self._buffer.extend(raw)
        while True:
            cut = self._buffer.find(b"\n")
            if cut < 0:
                break
            self._log(bytes(self._buffer[:cut]))
            del self._buffer[: cut + 1]
        return len(raw)

    def close(self) -> None:
        """Log whatever the last write left unterminated, then close."""
        if self._buffer:
            self._log(bytes(self._buffer))
            self._buffer.clear()
        super().close()

    @staticmethod
    def _log(raw: bytes) -> None:
        line = redact_credentials(raw.decode("utf-8", "replace")).strip()
        if line:
            logger.debug(line)


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
