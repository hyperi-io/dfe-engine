#  Project:      dfe-engine
#  File:         gitops/repo.py
#  Purpose:      Git mechanics for the deploy-specific gitops repo (dulwich)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Git mechanics for the deploy-specific gitops repo.

Uses ``dulwich.porcelain`` (the same library hyperi-pylib's DirectoryConfigStore
uses internally) so there is no shell-out to ``git``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from dulwich import porcelain
from scalo.logger import logger


@dataclass
class PublishResult:
    """Outcome of a publish: what changed and the resulting commit."""

    changed: bool
    files: list[str] = field(default_factory=list)
    commit_sha: str | None = None
    pushed: bool = False


class GitopsRepo:
    """A local working clone of the deploy-specific gitops repo.

    ``ensure()`` makes the working tree present (clone ``repo_url``, or init a
    fresh local repo when ``repo_url`` is empty). ``publish()`` writes a
    ``{path: content}`` artifact map, stages, commits only on change, and pushes
    when ``push`` is set and a remote is configured.
    """

    def __init__(
        self,
        *,
        local_path: str,
        repo_url: str = "",
        branch: str = "main",
        push: bool = False,
        username: str = "",
        token: str = "",
        author_name: str = "dfe-engine",
        author_email: str = "dfe-engine@hyperi.io",
    ) -> None:
        self._path = Path(local_path)
        self._repo_url = repo_url
        self._branch = branch
        self._push = push
        self._username = username
        self._token = token
        self._author = f"{author_name} <{author_email}>".encode()

    @property
    def path(self) -> Path:
        """Working-tree path of the local clone."""
        return self._path

    def _authed_url(self) -> str:
        """Embed HTTPS credentials in the remote URL.

        dulwich.porcelain clone/push take no username/password kwargs; HTTPS auth
        is carried in the URL. SSH URLs auth via the agent/keys (no creds here).
        The token lands in the local clone's origin config -- the working dir is
        in-cluster/local with restricted perms.
        """
        url = self._repo_url
        if self._username and url.startswith(("http://", "https://")):
            scheme, rest = url.split("://", 1)
            return f"{scheme}://{self._username}:{self._token}@{rest}"
        return url

    def ensure(self) -> Path:
        """Make the working tree present: clone, reuse, or init."""
        if (self._path / ".git").exists():
            return self._path
        if self._repo_url:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            logger.info("Cloning gitops deploy repo", repo_url=self._repo_url)
            porcelain.clone(
                self._authed_url(),
                str(self._path),
                branch=self._branch.encode(),
            )
            return self._path
        self._path.mkdir(parents=True, exist_ok=True)
        porcelain.init(str(self._path))
        return self._path

    def publish(self, artifacts: dict[str, str], message: str) -> PublishResult:
        """Write artifacts, stage, commit-if-changed, push-if-configured."""
        written: list[str] = []
        for rel, content in sorted(artifacts.items()):
            target = self._path / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8", newline="\n")
            written.append(rel)
            porcelain.add(str(self._path), paths=[str(target)])

        status = porcelain.status(str(self._path))
        staged = status.staged
        if not (staged["add"] or staged["modify"] or staged["delete"]):
            logger.info("Gitops repo unchanged; skipping commit")
            return PublishResult(changed=False, files=written)

        sha = porcelain.commit(
            str(self._path),
            message=message.encode(),
            author=self._author,
            committer=self._author,
        )
        sha_str = sha.decode() if isinstance(sha, bytes) else str(sha)

        pushed = False
        if self._push and self._repo_url:
            porcelain.push(
                str(self._path),
                self._authed_url(),
                f"refs/heads/{self._branch}".encode(),
            )
            pushed = True

        logger.info(
            "Published gitops artifacts",
            commit=sha_str,
            files=len(written),
            pushed=pushed,
        )
        return PublishResult(changed=True, files=written, commit_sha=sha_str, pushed=pushed)
