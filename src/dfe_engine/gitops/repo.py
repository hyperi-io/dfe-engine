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

Every remote op goes through :meth:`GitopsRepo._remote_op`: dulwich writes the
remote URL verbatim to its ``errstream`` and into its failure messages, and on the
HTTPS path that URL carries the deploy token (F-GITOPS-TOKEN).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeVar, cast

from dulwich import porcelain
from scalo.logger import logger

from .dulwich_auth import RedactingErrStream, redact_credentials, scrub_remote_credentials

T = TypeVar("T")


class GitopsRemoteError(RuntimeError):
    """A remote git op failed, with any URL credentials stripped from the message."""


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

    @property
    def has_remote(self) -> bool:
        """True when a remote deploy repo is configured (vs a local-only init)."""
        return bool(self._repo_url)

    def read_remote_file(self, rel: str) -> str | None:
        """Fetch the deploy repo and return ``rel``'s content at remote ``<branch>``.

        Read-only: fetches objects into the local store and reads the blob at the
        REMOTE branch head straight from the fetch result -- it never touches the
        working tree, makes a merge commit, or relies on remote-tracking refs (which
        a targeted fetch does not update). Used by the review-PR merge poll so a PR
        an operator merged on the forge is seen without recloning the pod. Returns
        None when there is no remote, the remote branch is unknown, or the path is
        absent in that tree.
        """
        if not self._repo_url:
            return None
        from dulwich.object_store import tree_lookup_path
        from dulwich.repo import Repo

        result = self._remote_op(
            lambda errstream: porcelain.fetch(
                str(self._path), self._authed_url(), errstream=errstream
            )
        )
        self._scrub_remote()
        head = result.refs.get(b"refs/heads/" + self._branch.encode())
        if head is None:
            return None
        with Repo(str(self._path)) as repo:
            try:
                commit = repo[head]
                _mode, blob_sha = tree_lookup_path(repo.get_object, commit.tree, rel.encode())
            except KeyError:
                return None
            return repo[blob_sha].data.decode("utf-8")

    def _splices_credentials(self) -> bool:
        """True when :meth:`_authed_url` embeds userinfo, so a clone can persist it."""
        return bool(self._username) and self._repo_url.startswith(("http://", "https://"))

    def _authed_url(self) -> str:
        """Embed HTTPS credentials in the remote URL.

        dulwich.porcelain clone/push take no username/password kwargs; HTTPS auth
        is carried in the URL. SSH URLs auth via the agent/keys (no creds here).
        The credentialed URL is supplied per op and never left in ``.git/config``
        -- :meth:`_scrub_remote` rewrites the stored remote back to the bare URL.
        """
        url = self._repo_url
        if self._splices_credentials():
            scheme, rest = url.split("://", 1)
            return f"{scheme}://{self._username}:{self._token}@{rest}"
        return url

    def _remote_op(self, op: Callable[[RedactingErrStream], T]) -> T:
        """Run a dulwich remote op with the URL's credentials kept out of every output.

        The op's progress/status goes to a redacting stream instead of stderr, and a
        failure is re-raised as :class:`GitopsRemoteError` with the userinfo stripped
        -- dulwich puts the URL as supplied in both. Chained ``from None`` on purpose:
        the original exception's own text is the thing carrying the token.
        """
        stream = RedactingErrStream()
        try:
            return op(stream)
        except Exception as exc:
            raise GitopsRemoteError(redact_credentials(str(exc))) from None
        finally:
            # The stream redacts a line at a time, so closing it flushes whatever
            # the op's last write left unterminated.
            stream.close()

    def _scrub_remote(self) -> None:
        """Rewrite the stored remote back to the credential-free URL.

        ``porcelain.clone`` persists the URL it cloned from, so a credentialed
        clone leaves the deploy token in ``.git/config`` in plaintext; every push
        and fetch re-supplies the credentials explicitly, so the stored remote
        never needs them. Best-effort: a clone that keeps its credential is a
        hardening miss, not a reason to fail the op.

        The guard is _authed_url's own condition: an empty token still splices
        ``user:@host``, so gating the scrub on the token left that on disk.
        """
        if not self._splices_credentials():
            return
        try:
            scrub_remote_credentials(self._path, self._repo_url)
        except Exception as exc:
            logger.warning(
                "Could not scrub the gitops clone's stored credentials",
                path=str(self._path),
                error=redact_credentials(str(exc)),
            )

    def ensure(self) -> Path:
        """Make the working tree present: clone, reuse, or init."""
        if (self._path / ".git").exists():
            # A clone that died between writing .git/config and its own scrub would
            # otherwise leave the token on disk for the life of the working tree.
            self._scrub_remote()
            return self._path
        if self._repo_url:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            logger.info("Cloning gitops deploy repo", repo_url=self._repo_url)
            self._remote_op(
                lambda errstream: porcelain.clone(
                    self._authed_url(),
                    str(self._path),
                    branch=self._branch.encode(),
                    errstream=errstream,
                )
            )
            self._scrub_remote()
            return self._path
        self._path.mkdir(parents=True, exist_ok=True)
        porcelain.init(str(self._path))
        return self._path

    def sync(self, *, discard_local: bool = False) -> bool:
        """Fast-forward the tracked branch to the remote's head.

        Every engine replica holds its own clone of one deploy repo, so the clone a
        write lands on is behind whenever another replica pushed first, and a commit
        made on that head is a non-fast-forward push. Returns True when the local
        branch moved. ``discard_local`` drops a local commit the remote never took
        (a rejected push) so the caller can re-apply the write on the remote head.
        """
        if not self._repo_url:
            return False
        from dulwich.graph import can_fast_forward
        from dulwich.repo import Repo

        result = self._remote_op(
            lambda errstream: porcelain.fetch(
                str(self._path), self._authed_url(), errstream=errstream
            )
        )
        self._scrub_remote()
        remote_head = result.refs.get(b"refs/heads/" + self._branch.encode())
        if remote_head is None:
            return False
        local = self.head_revision()
        if local == remote_head.decode():
            return False
        if local is not None and not discard_local:
            with Repo(str(self._path)) as repo:
                if not can_fast_forward(repo, local.encode(), remote_head):
                    raise GitopsRemoteError(
                        f"the deploy repo clone diverged from {self._branch}: local "
                        f"{local[:12]} is not behind remote {remote_head.decode()[:12]}"
                    )
        repo_path = str(self._path)
        porcelain.update_ref(repo_path, b"refs/heads/" + self._branch.encode(), remote_head)
        porcelain.reset(repo_path, "hard", remote_head)
        logger.info("Gitops clone fast-forwarded to the remote head", commit=remote_head.decode())
        return True

    def publish(
        self,
        artifacts: Mapping[str, str | bytes],
        message: str,
        deletions: list[str] | None = None,
        branch: str | None = None,
    ) -> PublishResult:
        """Write artifacts, optionally remove files, commit-if-changed, push-if-set.

        Text is written UTF-8 with LF endings; ``bytes`` is written verbatim, so a
        binary artefact is stored as itself rather than as an encoding of itself.

        ``branch`` selects the target:
        - ``None`` (default): commit straight onto the tracked branch and push it
          (the direct-to-main path; Argo auto-syncs).
        - a name: PR mode. Commit onto a short-lived ``branch`` OFF the current
          HEAD, push only that branch, and leave the tracked branch untouched
          locally and remotely -- a reviewer merges the PR. This is how a
          production+team write is kept off main (see gitcrud/routing.py).
        """
        # The write goes on the remote's head, never on whatever this clone last saw.
        if self._push and self._repo_url:
            self.sync()

        # Capture the base BEFORE staging so PR mode can restore the tracked
        # branch to it after committing. An empty repo has no base to branch from.
        base_head = self.head_revision()
        if branch and base_head is None:
            raise ValueError("cannot open a review branch: the deploy repo has no commits yet")

        sha_str, written = self._stage_and_commit(artifacts, deletions, message)
        if sha_str is None:
            logger.info("Gitops repo unchanged; skipping commit")
            return PublishResult(changed=False, files=written)

        if branch:
            # base_head is non-None here: the empty-repo case raised above.
            return self._route_to_branch(sha_str, branch, cast("str", base_head), written)

        pushed = False
        if self._push and self._repo_url:
            refspec = f"refs/heads/{self._branch}".encode()
            try:
                self._push_refspec(refspec)
            except GitopsRemoteError as exc:
                # The remote moved between the sync and the push, so the local commit
                # is orphaned: take the remote head and re-apply this write once. A
                # push the remote rejected for any other reason stays an error.
                if not self.sync(discard_local=True):
                    raise
                logger.warning(
                    "Gitops push rejected; re-applying the write on the remote head",
                    error=str(exc),
                )
                sha_str, written = self._stage_and_commit(artifacts, deletions, message)
                if sha_str is None:
                    logger.info("Gitops repo unchanged after the remote caught up")
                    return PublishResult(changed=False, files=written)
                self._push_refspec(refspec)
            pushed = True

        logger.info(
            "Published gitops artifacts",
            commit=sha_str,
            files=len(written),
            pushed=pushed,
        )
        return PublishResult(changed=True, files=written, commit_sha=sha_str, pushed=pushed)

    def _stage_and_commit(
        self,
        artifacts: Mapping[str, str | bytes],
        deletions: list[str] | None,
        message: str,
    ) -> tuple[str | None, list[str]]:
        """Write and stage the artifacts; commit when anything changed.

        Returns the commit SHA (None when nothing was staged) and the paths touched.
        """
        written: list[str] = []
        for rel, content in sorted(artifacts.items()):
            target = self._path / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                target.write_bytes(content)
            else:
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
            return None, written

        sha = porcelain.commit(
            str(self._path),
            message=message.encode(),
            author=self._author,
            committer=self._author,
        )
        return (sha.decode() if isinstance(sha, bytes) else str(sha)), written

    def _push_refspec(self, refspec: bytes) -> None:
        """Push one refspec to the configured remote, credentials never logged."""
        self._remote_op(
            lambda errstream: porcelain.push(
                str(self._path),
                self._authed_url(),
                refspec,
                errstream=errstream,
            )
        )

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
            self._push_refspec(side_ref + b":" + side_ref)
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
