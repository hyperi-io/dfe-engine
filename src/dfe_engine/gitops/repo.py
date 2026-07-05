#  Project:      dfe-engine
#  File:         gitops/repo.py
#  Purpose:      Git mechanics for the deploy-specific gitops repo (dulwich)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Git mechanics for the deploy-specific gitops repo.

Uses ``dulwich.porcelain`` (the same library scalo's DirectoryConfigStore
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


class RepoDirtyError(RuntimeError):
    """Raised when publish() finds pre-existing staged changes at entry.

    Sweeping leftovers into this publish's commit would record someone else's
    (or a crashed publish's) changes under the wrong actor - refuse instead.
    """


class PushError(RuntimeError):
    """Raised when the local commit landed but the push to the remote did not.

    Carries ``committed_sha`` so callers can tell a failed commit apart from a
    committed-locally-but-unpushed state (the clone is ahead of the remote).
    """

    def __init__(self, message: str, committed_sha: str) -> None:
        super().__init__(message)
        self.committed_sha = committed_sha


class PathEscapesRepoError(ValueError):
    """Raised when an artifact/deletion path would resolve outside the repo root.

    Resource names flow in from callers (GitCrud._rel builds ``dir/name.suffix``);
    a name carrying ``..``, an absolute marker or a NUL byte would let publish()
    write or unlink a file OUTSIDE the clone (F-GITCRUD-TRAVERSAL). Every path is
    contained under the repo root before any filesystem touch.
    """


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
        """Embed HTTPS credentials in the remote URL for a single git operation.

        dulwich.porcelain clone/push take no username/password kwargs; HTTPS auth
        is carried in the URL. SSH URLs auth via the agent/keys (no creds here).
        This value is passed EXPLICITLY to clone/push/fetch on each call and is
        never persisted: clone would otherwise write it into the local
        ``.git/config`` (remote.origin.url), so ensure() scrubs the stored remote
        back to the bare URL (F-GITOPS-TOKEN).
        """
        url = self._repo_url
        if self._username and url.startswith(("http://", "https://")):
            scheme, rest = url.split("://", 1)
            return f"{scheme}://{self._username}:{self._token}@{rest}"
        return url

    def _scrub_remote_credentials(self) -> None:
        """Rewrite remote.origin.url back to the bare, credential-free repo URL.

        porcelain.clone persists whatever URL it cloned from into the clone's
        ``.git/config``; for HTTPS token auth that _authed_url() carries
        ``username:token@`` in plaintext on the shared/in-cluster volume, readable
        by any co-located sidecar, exec shell or volume snapshot (F-GITOPS-TOKEN).
        Scrub it: push()/fetch() re-supply _authed_url() explicitly every call, so
        the stored remote never needs the credential. No-op when no token is held.
        """
        if not (self._username and self._token):
            return
        from dulwich.repo import Repo

        with Repo(str(self._path)) as repo:
            config = repo.get_config()
            config.set((b"remote", b"origin"), b"url", self._repo_url.encode())
            config.write_to_path()

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
            # Never leave the push token in .git/config (F-GITOPS-TOKEN).
            self._scrub_remote_credentials()
            return self._path
        self._path.mkdir(parents=True, exist_ok=True)
        porcelain.init(str(self._path))
        return self._path

    def publish(
        self,
        artifacts: dict[str, str],
        message: str,
        deletions: list[str] | None = None,
    ) -> PublishResult:
        """Write artifacts, optionally remove files, commit-if-changed, push-if-set.

        Refuses pre-existing staged changes (RepoDirtyError), rolls the touched
        paths back to HEAD if the write/stage/commit sequence fails partway, and
        surfaces a push failure as PushError carrying the local commit sha -
        after one fetch + fast-forward-only reconcile attempt (never a force).
        """
        self._refuse_dirty()
        changed, sha_str, written = self._apply_and_commit(artifacts, deletions or [], message)
        if not changed or sha_str is None:
            logger.info("Gitops repo unchanged; skipping commit")
            return PublishResult(changed=False, files=written)

        pushed = False
        if self._push and self._repo_url:
            try:
                self._push_branch()
            except porcelain.DivergedBranches:
                return self._reconcile_and_repush(
                    artifacts, deletions or [], message, sha_str, written
                )
            except Exception as exc:
                raise PushError(
                    f"push failed; local commit {sha_str} not pushed: {exc}", sha_str
                ) from exc
            pushed = True

        logger.info(
            "Published gitops artifacts",
            commit=sha_str,
            files=len(written),
            pushed=pushed,
        )
        return PublishResult(changed=True, files=written, commit_sha=sha_str, pushed=pushed)

    def _refuse_dirty(self) -> None:
        """Raise RepoDirtyError when the index already has staged entries."""
        staged = porcelain.status(str(self._path)).staged
        leftovers = [*staged["add"], *staged["modify"], *staged["delete"]]
        if leftovers:
            names = ", ".join(sorted(p.decode(errors="replace") for p in leftovers))
            raise RepoDirtyError(f"gitops repo has pre-existing staged changes: {names}")

    def _contained_target(self, rel: str) -> Path:
        """Resolve an artifact/deletion path under the repo root, refusing escapes.

        The single choke point every write and delete goes through: a ``rel`` with
        ``..``, an absolute marker or a NUL byte would otherwise land a file
        OUTSIDE the clone (F-GITCRUD-TRAVERSAL). NUL is checked first because
        ``Path.resolve`` raises a bare ValueError on an embedded null byte; then
        require the resolved candidate to stay within the resolved repo root,
        mirroring SchemaRegistry._yaml_path.
        """
        if "\x00" in rel:
            raise PathEscapesRepoError(f"artifact path has a NUL byte: {rel!r}")
        base = self._path.resolve(strict=False)
        candidate = (self._path / rel).resolve(strict=False)
        if not candidate.is_relative_to(base):
            raise PathEscapesRepoError(f"artifact path escapes repo root: {rel!r}")
        return self._path / rel

    def _apply_and_commit(
        self,
        artifacts: dict[str, str],
        deletions: list[str],
        message: str,
    ) -> tuple[bool, str | None, list[str]]:
        """Write + stage + commit-if-changed; returns (changed, sha, written).

        On any failure the touched paths are restored to HEAD (worktree and
        index) before re-raising, so a crashed publish never leaves staged
        leftovers for the next publish to sweep into an unrelated commit.
        """
        touched: list[str] = []
        try:
            written: list[str] = []
            for rel, content in sorted(artifacts.items()):
                target = self._contained_target(rel)
                touched.append(rel)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8", newline="\n")
                written.append(rel)
                porcelain.add(str(self._path), paths=[str(target)])

            for rel in sorted(deletions):
                target = self._contained_target(rel)
                if target.exists():
                    touched.append(rel)
                    # porcelain.remove deletes from the working tree AND stages removal.
                    porcelain.remove(str(self._path), paths=[str(target)])
                    written.append(rel)

            staged = porcelain.status(str(self._path)).staged
            if not (staged["add"] or staged["modify"] or staged["delete"]):
                return False, None, written

            sha = porcelain.commit(
                str(self._path),
                message=message.encode(),
                author=self._author,
                committer=self._author,
            )
            sha_str = sha.decode() if isinstance(sha, bytes) else str(sha)
            return True, sha_str, written
        except Exception:
            try:
                self._restore_to_head(touched)
            except Exception:
                logger.warning("Rollback of partial gitops publish failed", paths=touched)
            raise

    def _restore_to_head(self, rels: list[str]) -> None:
        """Restore the given paths to their HEAD state (worktree + index)."""
        if not rels:
            return
        from dulwich.object_store import tree_lookup_path
        from dulwich.repo import Repo

        in_head: list[str] = []
        with Repo(str(self._path)) as repo:
            try:
                head_tree = repo[repo.head()].tree
            except KeyError:
                head_tree = None  # empty repo: nothing was ever committed
            index = repo.open_index()
            index_dirty = False
            for rel in rels:
                present = False
                if head_tree is not None:
                    try:
                        tree_lookup_path(repo.object_store.__getitem__, head_tree, rel.encode())
                        present = True
                    except KeyError:
                        present = False
                if present:
                    in_head.append(rel)
                    continue
                # not in HEAD: drop the staged add and the written file
                if rel.encode() in index:
                    del index[rel.encode()]
                    index_dirty = True
                target = self._path / rel
                if target.exists():
                    target.unlink()
            if index_dirty:
                index.write()
        if in_head:
            porcelain.restore(
                str(self._path), list(in_head), source="HEAD", staged=True, worktree=True
            )

    def _push_branch(self) -> None:
        porcelain.push(
            str(self._path),
            self._authed_url(),
            f"refs/heads/{self._branch}".encode(),
        )

    def _reconcile_and_repush(
        self,
        artifacts: dict[str, str],
        deletions: list[str],
        message: str,
        local_sha: str,
        written: list[str],
    ) -> PublishResult:
        """One fetch + fast-forward-only reconcile, then a single re-push.

        Never force-pushes: when the remote head no longer contains our base
        commit the histories truly diverged, so we surface PushError (with the
        stranded local sha) instead of overwriting the remote.
        """
        from dulwich.graph import can_fast_forward
        from dulwich.repo import Repo

        logger.info("Push rejected; fetching remote for reconcile", commit=local_sha)
        try:
            fetched = porcelain.fetch(str(self._path), self._authed_url())
        except Exception as exc:
            raise PushError(
                f"push rejected and fetch failed; local commit {local_sha} not pushed: {exc}",
                local_sha,
            ) from exc
        remote_sha = (fetched.refs or {}).get(f"refs/heads/{self._branch}".encode())
        if remote_sha is None:
            raise PushError(
                f"push rejected and remote branch {self._branch} not found;"
                f" local commit {local_sha} not pushed",
                local_sha,
            )

        with Repo(str(self._path)) as repo:
            local = local_sha.encode()
            try:
                remote_contained = can_fast_forward(repo, remote_sha, local)
            except KeyError:
                remote_contained = False
            parents = repo[local].parents
            base = parents[0] if parents else None
            try:
                base_in_remote = base is not None and can_fast_forward(repo, base, remote_sha)
            except KeyError:
                base_in_remote = False

        if remote_contained:
            # remote head is already an ancestor of our commit: plain FF re-push
            try:
                self._push_branch()
            except Exception as exc:
                raise PushError(
                    f"re-push failed; local commit {local_sha} not pushed: {exc}", local_sha
                ) from exc
            return PublishResult(changed=True, files=written, commit_sha=local_sha, pushed=True)

        if not base_in_remote:
            raise PushError(
                f"remote {self._branch} diverged beyond fast-forward;"
                f" local commit {local_sha} not pushed",
                local_sha,
            )

        # remote moved on from our base: fast-forward the clone to the remote
        # head and rebuild this publish's commit on top of it (never a force)
        porcelain.reset(str(self._path), "hard", remote_sha)
        changed, sha_str, written = self._apply_and_commit(artifacts, deletions, message)
        if not changed or sha_str is None:
            # the fetched remote head already carries exactly this content
            logger.info("Gitops repo unchanged after reconcile; skipping commit")
            return PublishResult(changed=False, files=written)
        try:
            self._push_branch()
        except Exception as exc:
            raise PushError(
                f"re-push failed after reconcile; local commit {sha_str} not pushed: {exc}",
                sha_str,
            ) from exc
        logger.info(
            "Published gitops artifacts after reconcile",
            commit=sha_str,
            files=len(written),
            pushed=True,
        )
        return PublishResult(changed=True, files=written, commit_sha=sha_str, pushed=True)
