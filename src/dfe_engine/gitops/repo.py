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
from typing import cast

from dulwich import porcelain
from scalo.logger import logger


@dataclass
class PublishResult:
    """Outcome of a publish: what changed and the resulting commit."""

    changed: bool
    files: list[str] = field(default_factory=list)
    commit_sha: str | None = None
    pushed: bool = False
    # Set to the short-lived branch name when the commit was routed to a review
    # branch (PR mode) instead of the tracked branch. None => committed to main.
    branch: str | None = None


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

    @property
    def branch(self) -> str:
        """The tracked branch (commit/push target, and the PR base)."""
        return self._branch

    def head_revision(self) -> str | None:
        """Current HEAD commit SHA, or None for an empty repo (no commits yet).

        Used as the optimistic-concurrency version token: a read returns it, a
        write requires it, and a moved HEAD means a conflict.
        """
        from dulwich.repo import Repo

        try:
            with Repo(str(self._path)) as repo:
                return repo.head().decode()
        except (KeyError, FileNotFoundError):
            return None

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

    def publish(
        self,
        artifacts: dict[str, str],
        message: str,
        deletions: list[str] | None = None,
        branch: str | None = None,
    ) -> PublishResult:
        """Write artifacts, optionally remove files, commit-if-changed, push-if-set.

        ``branch`` selects the target:
        - ``None`` (default): commit straight onto the tracked branch and push it
          (the direct-to-main path; Argo auto-syncs).
        - a name: PR mode. Commit onto a short-lived ``branch`` OFF the current
          HEAD, push only that branch, and leave the tracked branch untouched
          locally and remotely -- a reviewer merges the PR. This is how a
          production+team write is kept off main (see gitcrud/routing.py).
        """
        # Capture the base BEFORE staging so PR mode can restore the tracked
        # branch to it after committing. An empty repo has no base to branch from.
        base_head = self.head_revision()
        if branch and base_head is None:
            raise ValueError("cannot open a review branch: the deploy repo has no commits yet")

        written: list[str] = []
        for rel, content in sorted(artifacts.items()):
            target = self._path / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8", newline="\n")
            written.append(rel)
            porcelain.add(str(self._path), paths=[str(target)])

        for rel in sorted(deletions or []):
            target = self._path / rel
            if target.exists():
                # porcelain.remove deletes from the working tree AND stages removal.
                porcelain.remove(str(self._path), paths=[str(target)])
                written.append(rel)

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

        if branch:
            # base_head is non-None here: the empty-repo case raised above.
            return self._route_to_branch(sha_str, branch, cast("str", base_head), written)

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

    def _route_to_branch(
        self, sha_str: str, branch: str, base_head: str, written: list[str]
    ) -> PublishResult:
        """Move the just-made commit onto a side branch, restore + reset the base.

        ``porcelain.commit`` advanced the CURRENT branch (HEAD) to ``sha_str``. We
        re-point that commit at ``refs/heads/<branch>``, wind the tracked branch
        back to ``base_head``, hard-reset the work tree, and push only the side
        branch. Net effect: the change lands on a review branch, main is untouched.
        No git CLI -- pure dulwich porcelain, same as the rest of this module.
        """
        repo_path = str(self._path)
        tracked_ref = b"refs/heads/" + porcelain.active_branch(repo_path)
        side_ref = f"refs/heads/{branch}".encode()
        porcelain.update_ref(repo_path, side_ref, sha_str.encode())
        porcelain.update_ref(repo_path, tracked_ref, base_head.encode())
        porcelain.reset(repo_path, "hard", base_head.encode())

        pushed = False
        if self._push and self._repo_url:
            porcelain.push(
                repo_path,
                self._authed_url(),
                side_ref + b":" + side_ref,
            )
            pushed = True

        logger.info(
            "Published gitops artifacts to a review branch (main untouched)",
            commit=sha_str,
            branch=branch,
            files=len(written),
            pushed=pushed,
        )
        return PublishResult(
            changed=True, files=written, commit_sha=sha_str, pushed=pushed, branch=branch
        )
