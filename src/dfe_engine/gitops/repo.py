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

Every remote op goes through :meth:`GitopsRepo._remote_op`: dulwich writes the
remote URL verbatim to its ``errstream`` and into its failure messages, and on the
HTTPS path that URL carries the deploy token (F-GITOPS-TOKEN).

A read's remote calls are held to ``REMOTE_HEAD_TIMEOUT_SECONDS`` and a failure
leaves the clone serving what it has. A write's fetch and push take the longer
``gitops.write`` timeout, and a transient failure is retried inside that budget
before the write raises :class:`GitopsUnavailableError`, which the API answers 503.
"""

import math
import os
import random
import re
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar, cast

from dulwich import porcelain
from scalo.logger import logger
from scalo.resilience import ReconnectingResilience, ResilienceConfig, ServiceUnavailable

from ..settings import GitopsWriteSettings
from .dulwich_auth import RedactingErrStream, redact_credentials, scrub_remote_credentials
from .metrics import GitopsMetrics, WriteOp

if TYPE_CHECKING:
    import urllib3
    from dulwich.client import FetchPackResult, LsRemoteResult, SendPackResult
    from dulwich.config import Config

T = TypeVar("T")

# Every API read pays a ref advertisement and may pay a fetch behind it, both under
# the tree lock, so a forge that drops packets rather than refusing them must not
# hold a request -- or every other reader -- for the OS connect timeout.
REMOTE_HEAD_TIMEOUT_SECONDS = 3.0

# Only the HTTP(S) client takes a pool manager, so only these schemes can be bounded.
_BOUNDABLE_SCHEMES = ("http://", "https://")

# urllib3 refuses a timeout of zero, which is what a retry started at the budget's end
# would otherwise get.
_MIN_ATTEMPT_TIMEOUT_SECONDS = 0.05

# dulwich reports a non-200 answer only in its message; these are the ones a forge or
# its proxy gives while restarting or overloaded.
_TRANSIENT_HTTP_STATUS = re.compile(r"unexpected http resp (?:408|429|5\d\d)\b")


def _as_path_bytes(host_path: str | bytes) -> bytes:
    """dulwich's clients take the remote path as bytes; the transport hands back either."""
    return host_path.encode() if isinstance(host_path, str) else host_path


def _is_transient(exc: BaseException, *, outer: BaseException | None = None) -> bool:
    """Whether a remote op failed in a way another attempt could fix.

    dulwich wraps a urllib3 failure in ``GitProtocolError``, so the chain is walked
    rather than the outer type alone: every ``__cause__``, and a ``__context__`` only
    where Python did not suppress it. The walk stops at ``outer`` -- the exception
    already being handled when the op began, a caller's or an earlier attempt's --
    and at any GitopsRemoteError below the top, because neither describes this
    failure. A refusal, a diverged ref, a missing repo or bad credentials are not
    transient: another attempt fails the same way.
    """
    import urllib3.exceptions
    from dulwich.errors import GitProtocolError, HangupException

    transient_types = (
        TimeoutError,
        ConnectionError,
        HangupException,
        urllib3.exceptions.TimeoutError,
        urllib3.exceptions.ProtocolError,
    )
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and current is not outer and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, GitopsRemoteError):
            return current is exc and current.transient
        if isinstance(current, transient_types):
            return True
        if isinstance(current, GitProtocolError) and _TRANSIENT_HTTP_STATUS.search(str(current)):
            return True
        if current.__cause__ is not None:
            current = current.__cause__
        elif current.__suppress_context__:
            current = None
        else:
            current = current.__context__
    return False


def _jittered_sleep(seconds: float) -> None:
    """Sleep a random share of the back-off, so replicas that failed together retry apart."""
    time.sleep(random.uniform(seconds / 2, seconds))  # noqa: S311 - schedules a retry, not crypto


# The clones whose head this scope has already taken; None outside a scope.
_READ_SCOPE: ContextVar[set[GitopsRepo] | None] = ContextVar("dfe_gitops_read_scope", default=None)


@contextmanager
def read_scope() -> Iterator[None]:
    """Answer every read in this block from one view of the deploy repo.

    A request reads many documents and a listing reads them all, so without this
    each one pays its own ref advertisement. The first read takes the remote's head
    and the rest of the block reuses it; the next block asks again. Outside a scope
    -- the CLI, background work -- every read asks, which is what they want.
    """
    # Restored by setting the previous value, not by resetting a token: a token
    # reset raises when the block exits in a different context (see tags_context).
    previous = _READ_SCOPE.get()
    _READ_SCOPE.set(set())
    try:
        yield
    finally:
        _READ_SCOPE.set(previous)


class GitopsRemoteError(RuntimeError):
    """A remote git op failed, with any URL credentials stripped from the message.

    Attributes:
        transient: Whether another attempt could succeed -- a timeout, a refused or
            reset connection, a 5xx -- rather than a failure retrying cannot fix.
    """

    def __init__(self, message: str, *, transient: bool = False) -> None:
        super().__init__(message)
        self.transient = transient


class GitopsUnavailableError(GitopsRemoteError, ServiceUnavailable):
    """A write's remote call kept failing transiently until its retry budget ran out.

    The API answers it 503 with ``Retry-After``: the deploy repo is unreachable, the
    request was not wrong.

    Attributes:
        remote: The deploy repo URL, credentials stripped.
        retry_after_seconds: How long a caller should wait before trying again.
    """

    def __init__(self, message: str, *, waking: bool = False) -> None:
        super().__init__(message, transient=True)
        self.waking = waking
        self.remote = ""
        self.retry_after_seconds = 1


class GitopsDivergedError(GitopsRemoteError):
    """This clone holds a commit the remote branch does not, so it cannot fast-forward.

    Attributes:
        local: The clone's head commit SHA.
        remote: The remote branch's head commit SHA.
    """

    def __init__(self, branch: str, local: str, remote: str) -> None:
        super().__init__(
            f"the deploy repo clone diverged from {branch}: local {local[:12]} "
            f"is not behind remote {remote[:12]}"
        )
        self.local = local
        self.remote = remote


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


@dataclass(slots=True)
class _Batch:
    """The net writes of an open batch, and the subject of each write that changed a file."""

    message: str
    owner: int
    artifacts: dict[str, str | bytes] = field(default_factory=dict)
    deletions: set[str] = field(default_factory=set)
    subjects: list[str] = field(default_factory=list)

    def record(self, artifacts: Mapping[str, str | bytes], deletions: list[str]) -> None:
        """Fold one write into the net set: the last write or removal of a path wins."""
        for rel, content in artifacts.items():
            self.artifacts[rel] = content
            self.deletions.discard(rel)
        for rel in deletions:
            self.deletions.add(rel)
            self.artifacts.pop(rel, None)

    def commit_message(self) -> str:
        """The batch's own subject, with every write it carries listed in the body."""
        subjects = list(dict.fromkeys(s for s in self.subjects if s))
        if not subjects:
            return self.message
        return self.message + "\n\n" + "\n".join(f"- {s}" for s in subjects)


class GitopsRepo:
    """A local working clone of the deploy-specific gitops repo.

    ``ensure()`` makes the working tree present (clone ``repo_url``, or init a
    fresh local repo when ``repo_url`` is empty). ``publish()`` writes a
    ``{path: content}`` artifact map, stages, commits only on change, and pushes
    when ``push`` is set and a remote is configured. ``write`` bounds and retries
    the fetch and push a write makes; None takes the ``gitops.write`` defaults.
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
        metrics: GitopsMetrics | None = None,
        write: GitopsWriteSettings | None = None,
    ) -> None:
        self._path = Path(local_path)
        self._repo_url = repo_url
        self._branch = branch
        self._push = push
        self._username = username
        self._token = token
        self._author = f"{author_name} <{author_email}>".encode()
        self._metrics = metrics or GitopsMetrics()
        self._write = write if write is not None else GitopsWriteSettings()
        # No pooled client to rebuild: every dulwich op builds its own, so a retry
        # only backs off.
        self._write_resilience = ReconnectingResilience(
            ResilienceConfig(
                wait_initial=self._write.wait_initial,
                wait_max=self._write.wait_max,
                wait_multiplier=self._write.wait_multiplier,
                budget_seconds=self._write.budget_seconds,
                waking_budget_seconds=self._write.budget_seconds,
            ),
            name="gitops deploy repo",
            is_transient=lambda exc: isinstance(exc, GitopsRemoteError) and exc.transient,
            is_reconnectable=lambda _exc: False,
            reconnect=lambda: None,
            unavailable_exc=GitopsUnavailableError,
            sleep=_jittered_sleep,
        )
        # The working tree: held to change it or read it, never across a push.
        self._lock = threading.RLock()
        # One writer at a time, push included: a reset between a commit and its push
        # drops the commit, so anything that resets the tree takes this first.
        self._write_lock = threading.RLock()
        # Set between a tracked-branch commit and the end of its push.
        self._pushing = False
        # How deep this thread is in read_locked blocks: a nested read must not wait for
        # the writer lock while holding the tree lock a writer is waiting on.
        self._reads = threading.local()
        # A refresh runs on every read, so an unreachable remote is reported on the
        # transition rather than once per read.
        self._refresh_failed = False
        # The open batch() block's writes.
        self._batch: _Batch | None = None

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
        except KeyError, FileNotFoundError:
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
        # A retry runs inside scalo's handler for the first failure, and a caller may
        # call from inside its own handler; neither exception is this op's.
        outer = sys.exception()
        stream = RedactingErrStream()
        try:
            return op(stream)
        except Exception as exc:
            raise GitopsRemoteError(
                redact_credentials(str(exc)), transient=_is_transient(exc, outer=outer)
            ) from None
        finally:
            # The stream redacts a line at a time, so closing it flushes whatever
            # the op's last write left unterminated.
            stream.close()

    def _write_op(self, op: WriteOp, call: Callable[[RedactingErrStream, float], T]) -> T:
        """Run a write's remote call, retrying a transient failure inside the write budget.

        ``call`` takes the stream and the timeout for that attempt. The budget runs from
        the first failure, as scalo's retry loop counts it, and a retry's timeout is
        capped at what is left of it, so the retries end with the budget.

        Raises:
            GitopsUnavailableError: every attempt failed transiently until the budget ran out.
            GitopsRemoteError: a failure another attempt cannot fix, raised at once.
        """
        deadline: float | None = None

        def attempt() -> T:
            nonlocal deadline
            timeout = self._write.timeout_seconds
            if deadline is not None:
                self._metrics.write_retry(op, "retried")
                left = max(deadline - time.monotonic(), _MIN_ATTEMPT_TIMEOUT_SECONDS)
                timeout = min(timeout, left)
            try:
                return self._remote_op(lambda errstream: call(errstream, timeout))
            except GitopsRemoteError:
                if deadline is None:
                    deadline = time.monotonic() + self._write.budget_seconds
                raise

        try:
            return self._write_resilience.run(attempt)
        except GitopsUnavailableError as exc:
            self._metrics.write_retry(op, "exhausted")
            exc.remote = redact_credentials(self._repo_url)
            # About one back-off step: the pace this clone was retrying at itself.
            exc.retry_after_seconds = max(1, math.ceil(self._write.wait_max))
            raise

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

    def remote_head(self) -> str | None:
        """The remote branch's head SHA, read without fetching a single object.

        One ref advertisement, so a reader can tell whether its clone is behind
        before paying for a fetch. None when there is no remote configured or the
        remote does not carry the tracked branch.

        Bounded by ``REMOTE_HEAD_TIMEOUT_SECONDS`` on the HTTP(S) transport, and a
        remote that exceeds it raises :class:`GitopsRemoteError` -- the same signal
        an unreachable forge already gives, so :meth:`refresh` serves this clone.
        """
        if not self._repo_url:
            return None
        result = self._remote_op(lambda _errstream: self._ls_remote())
        head = result.refs.get(b"refs/heads/" + self._branch.encode())
        return head.decode() if head is not None else None

    def _ls_remote(self) -> LsRemoteResult:
        """List the remote's refs, with the HTTP(S) transport held to a timeout.

        ``porcelain.ls_remote`` takes no timeout, so the client is built here the
        way porcelain builds it and handed a bounded pool manager. An SSH or local
        remote keeps dulwich's own behaviour.
        """
        from dulwich.client import get_transport_and_path
        from dulwich.config import StackedConfig, env_config

        url = self._authed_url()
        if not url.startswith(_BOUNDABLE_SCHEMES):
            return porcelain.ls_remote(url)

        config = StackedConfig.default()
        env_override = env_config(os.environ)
        if env_override is not None:
            config.backends.insert(0, env_override)
        client, host_path = get_transport_and_path(
            url, config=config, pool_manager=self._bounded_pool(config, REMOTE_HEAD_TIMEOUT_SECONDS)
        )
        return client.get_refs(_as_path_bytes(host_path))

    def _fetch(self, errstream: RedactingErrStream, *, timeout: float) -> FetchPackResult:
        """Fetch objects into the local store, with the HTTP(S) transport bounded.

        ``porcelain.fetch`` takes no timeout either, and a read runs it under the tree
        lock, so a forge that stops answering part way through would hold every
        other reader with it. This is porcelain's body for the shape this class uses
        -- a URL remote, which imports no remote-tracking refs and needs no reflog
        entry.
        """
        from dulwich.client import get_transport_and_path
        from dulwich.gc import maybe_auto_gc
        from dulwich.repo import Repo

        url = self._authed_url()
        if not url.startswith(_BOUNDABLE_SCHEMES):
            return porcelain.fetch(str(self._path), url, errstream=errstream)

        with Repo(str(self._path)) as repo:
            config = repo.get_config_stack()
            client, host_path = get_transport_and_path(
                url, config=config, pool_manager=self._bounded_pool(config, timeout)
            )
            result = client.fetch(_as_path_bytes(host_path), repo, progress=errstream.write)
            # porcelain.fetch ends on this, and a clone that lives as long as the pod
            # would otherwise accumulate loose objects fetch after fetch.
            maybe_auto_gc(repo)
            return result

    def _send_pack(
        self, refspec: bytes, errstream: RedactingErrStream, *, timeout: float
    ) -> SendPackResult:
        """Push one refspec, with the HTTP(S) transport held to ``timeout``."""
        from dulwich.repo import Repo

        url = self._authed_url()
        if not url.startswith(_BOUNDABLE_SCHEMES):
            return porcelain.push(str(self._path), url, refspec, errstream=errstream)
        with Repo(str(self._path)) as repo:
            pool = self._bounded_pool(repo.get_config_stack(), timeout)
        return porcelain.push(str(self._path), url, refspec, errstream=errstream, pool_manager=pool)

    def _bounded_pool(self, config: Config, timeout: float) -> urllib3.PoolManager:
        """A urllib3 manager whose connect and read both give up at ``timeout``.

        The read timeout is between reads rather than across the whole transfer, so
        a large pack that keeps moving is not at risk -- only one that stops.
        """
        from dulwich.client import default_urllib3_manager

        # base_url is the credential-free URL: it only selects the http.* config
        # sections and the proxy-bypass decision, neither of which wants the token.
        manager = default_urllib3_manager(config, base_url=self._repo_url, timeout=timeout)
        # urllib3 retries a failed connect three times by default, which would make
        # the real bound four times the timeout.
        manager.connection_pool_kw["retries"] = False
        return manager

    def refresh(self) -> bool:
        """Take the remote's head when it has moved; returns whether this clone moved.

        Every engine replica reads its OWN clone, so a resource another replica wrote
        is invisible here until this runs -- the read half of read-your-writes across
        replicas. Cheap when nothing moved: one ref advertisement and no fetch.

        Inside a :func:`read_scope` the head is taken once and the rest of the scope
        reuses it, so a listing of N documents costs one round trip rather than N; a
        publish reopens the check, and a remote this clone cannot reach costs one
        failed connection per scope instead of one per read.

        Only a pushing clone follows the remote: one holding a commit it never pushed
        is the sole copy of it, and a reset would destroy it. Best-effort -- a remote
        this clone cannot reach, or one slower than ``REMOTE_HEAD_TIMEOUT_SECONDS``,
        leaves it serving what it already has, because a read must not fail on a
        forge blip.
        """
        pending = self._pending_remote_head()
        if pending is None:
            return False
        with self._write_lock, self._lock:
            return self._take_head(pending)

    @contextmanager
    def read_locked(self) -> Iterator[None]:
        """Hold the working tree still while the caller reads it.

        :meth:`publish` stages and commits under this lock and a refresh hard-resets
        under it, so a read that walks the tree outside the block can miss a file
        that exists or load one mid-rewrite. A push runs outside it, so a read never
        waits for one unless the remote holds a commit this clone must take first.
        Never publish inside the block: a write takes the writer lock before this one.

        The ref advertisement runs BEFORE the lock is taken: a forge slow to answer
        would otherwise queue every reader behind one network call.
        """
        depth = getattr(self._reads, "depth", 0)
        # The outer block took the head already, and waiting here could deadlock.
        pending = self._pending_remote_head() if depth == 0 else None
        if pending is not None:
            with self._write_lock, self._lock:
                self._take_head(pending)
        with self._lock:
            self._reads.depth = depth + 1
            try:
                yield
            finally:
                self._reads.depth = depth

    def _pending_remote_head(self) -> str | None:
        """The remote head this clone has not taken yet, or None to stay put.

        Runs the ref advertisement, so callers take the tree lock after it, not
        around it. None inside an open batch, whose staged writes a reset would throw
        away, and while a push is taking this clone past the remote's head, which a
        reset would drop mid-push.
        """
        batch = self._batch
        if batch is not None and batch.owner == threading.get_ident():
            return None
        if not self._pushes():
            return None
        scope = _READ_SCOPE.get()
        if scope is not None:
            if self in scope:
                return None
            scope.add(self)
        try:
            remote = self.remote_head()
        except GitopsRemoteError as exc:
            self._note_unreachable(exc)
            return None
        if remote is None or remote == self.head_revision() or self._pushing_past(remote):
            self._note_reachable()
            return None
        return remote

    def _pushing_past(self, remote: str) -> bool:
        """Whether a push in flight carries everything at ``remote`` already."""
        if not self._pushing:
            return False
        head = self.head_revision()
        if head is None:
            return False
        return self._descends(head.encode(), remote.encode())

    def _descends(self, head: bytes, ancestor: bytes) -> bool:
        """Whether ``head``'s history holds ``ancestor`` (or is it).

        False for a commit this clone has never fetched: it cannot prove the history,
        and every caller treats an unproven one as not held.
        """
        from dulwich.graph import can_fast_forward
        from dulwich.objects import ObjectID
        from dulwich.repo import Repo

        with Repo(str(self._path)) as repo:
            try:
                return can_fast_forward(repo, ObjectID(ancestor), ObjectID(head))
            except KeyError:
                return False

    def _take_head(self, remote: str) -> bool:
        """Reset onto ``remote`` and return whether this clone moved.

        Caller holds the writer lock, then the tree lock.
        """
        # Re-checked under the lock: a publish may have taken this head.
        if remote == self.head_revision():
            self._note_reachable()
            return False
        try:
            self._settle_onto(self._fetch_head(write=False))
        except GitopsRemoteError as exc:
            self._note_unreachable(exc)
            return False
        self._note_reachable()
        return True

    def _note_unreachable(self, exc: GitopsRemoteError) -> None:
        """Report a refresh failure once, not once per read."""
        if self._refresh_failed:
            return
        self._refresh_failed = True
        logger.warning(
            "Cannot reach the deploy repo: serving this clone until it is back",
            branch=self._branch,
            error=str(exc),
        )

    def _note_reachable(self) -> None:
        """Close the warning a run of failed refreshes opened."""
        if not self._refresh_failed:
            return
        self._refresh_failed = False
        logger.info("Deploy repo reachable again; reads are current", branch=self._branch)

    def _fetch_head(self, *, write: bool) -> bytes | None:
        """Fetch the remote's objects and return its tracked branch head.

        Every engine replica holds its own clone of one deploy repo, so the clone a
        write lands on is behind whenever another replica pushed first, and a commit
        made on that head is a non-fast-forward push. None when the remote does not
        carry the branch.

        A read holds the tree lock across this, so its fetch keeps the short read
        bound and fails once. A write runs it under the writer lock alone -- readers
        carry on while a slow forge answers -- with the write timeout and retries.
        Caller holds the writer lock.

        Raises:
            GitopsUnavailableError: a write's fetch ran out of retry budget.
            GitopsRemoteError: the fetch failed.
        """
        if write:
            result = self._write_op(
                "fetch", lambda errstream, timeout: self._fetch(errstream, timeout=timeout)
            )
        else:
            result = self._remote_op(
                lambda errstream: self._fetch(errstream, timeout=REMOTE_HEAD_TIMEOUT_SECONDS)
            )
        self._scrub_remote()
        return result.refs.get(b"refs/heads/" + self._branch.encode())

    def _move_onto(self, remote_head: bytes | None, *, discard_local: bool) -> bool:
        """Fast-forward the tracked branch to a fetched remote head; returns whether it moved.

        ``discard_local`` drops a local commit the remote never took (a rejected push)
        so the caller can re-apply the write on the remote head. Caller holds the
        writer lock, then the tree lock.

        Raises:
            GitopsDivergedError: this clone holds a commit the remote does not, and
                ``discard_local`` is unset.
        """
        if remote_head is None:
            return False
        from dulwich.graph import can_fast_forward
        from dulwich.repo import Repo

        local = self.head_revision()
        if local == remote_head.decode():
            return False
        if local is not None and not discard_local:
            with Repo(str(self._path)) as repo:
                if not can_fast_forward(repo, local.encode(), remote_head):
                    raise GitopsDivergedError(self._branch, local, remote_head.decode())
        repo_path = str(self._path)
        porcelain.update_ref(repo_path, b"refs/heads/" + self._branch.encode(), remote_head)
        porcelain.reset(repo_path, "hard", remote_head)
        logger.info("Gitops clone fast-forwarded to the remote head", commit=remote_head.decode())
        return True

    @contextmanager
    def batch(self, message: str) -> Iterator[None]:
        """Land every publish in the block as ONE commit and ONE push.

        Each publish inside the block writes and stages its files as usual, so a read
        in the same block sees them, but nothing is committed until the block exits.
        The commit carries ``message``, with the subject of every write that changed a
        file listed in its body. The block holds the working tree until its commit and
        the writer lock until its push, and a read in the block never refreshes.

        An exception inside the block discards everything it staged, so the deploy
        repo takes all of the block or none of it. A write routed to a review branch
        needs a commit to cut the branch from, so it lands what the block holds first
        and the rest of the block goes into a second commit. A nested block joins the
        one already open.
        """
        with self._write_lock:
            if self._batch is not None:
                yield
                return
            # Taken once here, because no read or write inside the block refreshes.
            remote_head = self._fetch_head(write=True) if self._pushes() else None
            with self._lock:
                self._settle_onto(remote_head)
                self._batch = _Batch(message=message, owner=threading.get_ident())
                try:
                    yield
                except BaseException:
                    batch, self._batch = self._batch, None
                    self._discard_staged(batch)
                    self._metrics.batch("discarded")
                    raise
                batch, self._batch = self._batch, None
            try:
                landed = self._land(batch)
            except BaseException:
                self._metrics.batch("failed")
                raise
            finally:
                scope = _READ_SCOPE.get()
                if scope is not None:
                    scope.discard(self)
            self._metrics.batch("committed" if landed.changed else "unchanged")

    def _land(self, batch: _Batch) -> PublishResult:
        """Commit what a batch staged and push it. Caller holds the writer lock.

        The batch keeps its net writes so a push that loses the race to another
        replica re-applies all of them on the remote's new head.
        """
        with self._lock:
            if not self._has_staged():
                return PublishResult(changed=False)
            sha = self._commit(batch.commit_message())
            self._pushing = self._pushes()
        written = sorted([*batch.artifacts, *batch.deletions])
        return self._push_tracked(
            sha, written, dict(batch.artifacts), sorted(batch.deletions), batch.commit_message()
        )

    def _has_staged(self) -> bool:
        """Whether the index differs from HEAD."""
        staged = porcelain.status(str(self._path)).staged
        return bool(staged["add"] or staged["modify"] or staged["delete"])

    def _discard_staged(self, batch: _Batch) -> None:
        """Put the tree and index back on HEAD, dropping what a batch staged."""
        head = self.head_revision()
        if head is not None:
            porcelain.reset(str(self._path), "hard", head.encode())
            return
        # An empty repo has no commit to reset onto, and nothing the batch wrote was
        # there before it.
        for rel in batch.artifacts:
            target = self._path / rel
            if target.exists():
                porcelain.remove(str(self._path), paths=[str(target)], cached=True)
                target.unlink()

    def _split_batch(self) -> None:
        """Land what the open batch holds, and open a fresh one for the rest of the block."""
        batch = cast("_Batch", self._batch)
        self._batch = None
        try:
            self._land(batch)
        finally:
            self._batch = _Batch(message=batch.message, owner=batch.owner)
        self._metrics.batch("split")

    def _stage_into_batch(
        self,
        artifacts: Mapping[str, str | bytes],
        message: str,
        deletions: list[str] | None,
    ) -> PublishResult:
        """Stage one write into the open batch, whose commit waits for the block to end."""
        batch = cast("_Batch", self._batch)
        changed = self._differs(artifacts, deletions)
        written = self._stage(artifacts, deletions)
        batch.record(artifacts, list(deletions or []))
        if changed:
            batch.subjects.append(message.split("\n", 1)[0])
        return PublishResult(changed=changed, files=written)

    def _differs(self, artifacts: Mapping[str, str | bytes], deletions: list[str] | None) -> bool:
        """Whether writing these would change the working tree."""
        for rel, content in artifacts.items():
            target = self._path / rel
            data = content if isinstance(content, bytes) else content.encode("utf-8")
            if not target.is_file() or target.read_bytes() != data:
                return True
        return any((self._path / rel).exists() for rel in deletions or [])

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
        # The tree lock is taken inside for the local work only, so a read waits for
        # the commit and never for the fetch or the push.
        with self._write_lock:
            try:
                return self._publish_locked(artifacts, message, deletions, branch)
            finally:
                # This clone just wrote, so the next read in this scope asks the
                # remote again instead of reusing the head from before the write.
                scope = _READ_SCOPE.get()
                if scope is not None:
                    scope.discard(self)

    def push_local_commits(self) -> PublishResult:
        """Push commits this clone made with push off, as a fast-forward of the remote.

        :meth:`publish` drops a local commit the remote lacks, because on the write
        path that is a push which lost its race and whose caller re-applies it. A
        commit made here on purpose, such as a break-glass edit, has no one to
        re-apply it, so this pushes it instead, and refuses rather than drop it when
        the remote has moved on.

        Raises:
            GitopsDivergedError: the remote holds commits this clone does not; nothing
                was pushed and the local commits are kept.
            GitopsUnavailableError: the forge stayed unreachable for the write budget.
            GitopsRemoteError: the fetch or the push failed.
        """
        if not self._repo_url:
            return PublishResult(changed=False)
        with self._write_lock:
            remote_head = self._fetch_head(write=True)
            with self._lock:
                local = self.head_revision()
                if local is None or (remote_head is not None and remote_head.decode() == local):
                    return PublishResult(changed=False)
                if remote_head is not None and not self._descends(local.encode(), remote_head):
                    raise GitopsDivergedError(self._branch, local, remote_head.decode())
                self._pushing = True
            try:
                self._push_refspec(f"refs/heads/{self._branch}".encode())
            finally:
                self._pushing = False
        logger.info("Pushed local gitops commits", commit=local, branch=self._branch)
        return PublishResult(changed=True, commit_sha=local, pushed=True)

    def _publish_locked(
        self,
        artifacts: Mapping[str, str | bytes],
        message: str,
        deletions: list[str] | None,
        branch: str | None,
    ) -> PublishResult:
        """The publish body. Caller holds the writer lock."""
        if self._batch is not None:
            if not branch:
                with self._lock:
                    return self._stage_into_batch(artifacts, message, deletions)
            self._split_batch()

        # The write goes on the remote's head, never on whatever this clone last saw.
        remote_head = self._fetch_head(write=True) if self._pushes() else None
        with self._lock:
            self._settle_onto(remote_head)

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
                # base_head is non-None here: the empty-repo case raised above. Moved
                # under the tree lock, so no read sees the review commit on main.
                self._move_to_review_branch(sha_str, branch, cast("str", base_head))
            else:
                self._pushing = self._pushes()

        if branch:
            return self._push_review_branch(sha_str, branch, written)
        return self._push_tracked(sha_str, written, artifacts, deletions, message)

    def _push_tracked(
        self,
        sha_str: str,
        written: list[str],
        artifacts: Mapping[str, str | bytes],
        deletions: list[str] | None,
        message: str,
    ) -> PublishResult:
        """Push a tracked-branch commit, re-applying the write once if the remote moved.

        A refused push is re-applied only when the remote's history lacks the commit.
        When it holds it, the push landed and only its answer was lost, and a peer may
        already have written on top: re-applying would revert that write.

        Caller holds the writer lock and not the tree lock, so reads carry on over
        the committed tree while the push is in flight.
        """
        if not self._pushes():
            logger.info(
                "Published gitops artifacts", commit=sha_str, files=len(written), pushed=False
            )
            return PublishResult(changed=True, files=written, commit_sha=sha_str)
        refspec = f"refs/heads/{self._branch}".encode()
        try:
            try:
                self._push_refspec(refspec)
            except GitopsUnavailableError:
                # The forge stayed unreachable for the whole budget, so a re-fetch
                # would only spend another one before failing the same way.
                raise
            except GitopsRemoteError as exc:
                remote_head = self._fetch_head(write=True)
                reapplied: tuple[str | None, list[str]] | None = None
                with self._lock:
                    if remote_head is not None and self._descends(remote_head, sha_str.encode()):
                        self._move_onto(remote_head, discard_local=False)
                        logger.info(
                            "Gitops push had landed; took the remote head past it",
                            commit=sha_str,
                            remote=remote_head.decode(),
                        )
                    elif self._move_onto(remote_head, discard_local=True):
                        # The remote moved between the fetch and the push without this
                        # commit, so it is orphaned: re-apply the write on the new head.
                        logger.warning(
                            "Gitops push rejected; re-applying the write on the remote head",
                            error=str(exc),
                        )
                        reapplied = self._stage_and_commit(artifacts, deletions, message)
                    else:
                        # A push the remote rejected for any other reason stays an error.
                        raise
                if reapplied is not None:
                    sha, written = reapplied
                    if sha is None:
                        logger.info("Gitops repo unchanged after the remote caught up")
                        return PublishResult(changed=False, files=written)
                    sha_str = sha
                    self._push_refspec(refspec)
        finally:
            self._pushing = False

        logger.info(
            "Published gitops artifacts",
            commit=sha_str,
            files=len(written),
            pushed=True,
        )
        return PublishResult(changed=True, files=written, commit_sha=sha_str, pushed=True)

    def _pushes(self) -> bool:
        """Whether this clone pushes to a remote, and so writes on the remote's head."""
        return self._push and bool(self._repo_url)

    def _settle_onto(self, remote_head: bytes | None) -> None:
        """Fast-forward onto a fetched remote head, dropping a local commit it never took.

        A push that loses the compare-and-swap leaves this clone holding a commit the
        remote refused, and without this every later write on the replica fails against
        it. Caller holds the writer lock, then the tree lock.
        """
        try:
            self._move_onto(remote_head, discard_local=False)
        except GitopsDivergedError as exc:
            logger.warning(
                "Gitops clone diverged from the deploy repo: discarding the local commit "
                "the remote never took -- its content was a seed or a write a caller "
                "re-applies -- and writing on the remote head",
                branch=self._branch,
                local=exc.local,
                remote=exc.remote,
            )
            self._move_onto(remote_head, discard_local=True)

    def _stage(
        self,
        artifacts: Mapping[str, str | bytes],
        deletions: list[str] | None,
    ) -> list[str]:
        """Write and stage the artifacts, and stage the removals. Returns the paths touched."""
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
                # Unstaged by hand: porcelain.remove refuses a file whose staged content
                # differs from HEAD, which is what an earlier write in a batch leaves.
                porcelain.remove(str(self._path), paths=[str(target)], cached=True)
                target.unlink()
                written.append(rel)
        return written

    def _stage_and_commit(
        self,
        artifacts: Mapping[str, str | bytes],
        deletions: list[str] | None,
        message: str,
    ) -> tuple[str | None, list[str]]:
        """Write and stage the artifacts; commit when anything changed.

        Returns the commit SHA (None when nothing was staged) and the paths touched.
        """
        written = self._stage(artifacts, deletions)
        if not self._has_staged():
            return None, written
        return self._commit(message), written

    def _commit(self, message: str) -> str:
        """Commit the index as it stands and return the new commit's SHA."""
        sha = porcelain.commit(
            str(self._path),
            message=message.encode(),
            author=self._author,
            committer=self._author,
        )
        return sha.decode() if isinstance(sha, bytes) else str(sha)

    def _push_refspec(self, refspec: bytes) -> None:
        """Push one refspec as a compare-and-swap, credentials never logged.

        The update carries the ref's SHA as this clone last saw it, so the remote
        applies it only while the ref still holds that SHA -- another replica's push
        makes it stale, and the deploy repo keeps what it already has.

        A transient failure pushes the same commit again, never a new one. When the
        remote took the first attempt and only its answer was lost, the retry finds
        the ref already at this commit and sends nothing; when a peer has written on
        top since, the retry is refused and :meth:`_push_tracked` sees the commit in
        the remote's history.

        Raises:
            GitopsUnavailableError: the push ran out of retry budget.
            GitopsRemoteError: the op failed, or the remote refused a ref.
        """
        result = self._write_op(
            "push",
            lambda errstream, timeout: self._send_pack(refspec, errstream, timeout=timeout),
        )
        self._raise_on_refused(result)

    def _raise_on_refused(self, result: SendPackResult) -> None:
        """Turn a ref the remote refused into an error.

        ``porcelain.push`` writes a refused ref to its errstream and returns normally,
        so an unread ``ref_status`` is a write the remote never took reported to the
        caller as a success.

        Raises:
            GitopsRemoteError: at least one ref carries a refusal.
        """
        refused = sorted(
            f"{ref.decode()}: {status}"
            for ref, status in (result.ref_status or {}).items()
            if status is not None
        )
        if refused:
            raise GitopsRemoteError(
                redact_credentials("the remote refused the push -- " + "; ".join(refused))
            )

    def _move_to_review_branch(self, sha_str: str, branch: str, base_head: str) -> None:
        """Move the just-made commit onto a side branch, restore + reset the base.

        ``porcelain.commit`` advanced the CURRENT branch (HEAD) to ``sha_str``. We
        re-point that commit at ``refs/heads/<branch>``, wind the tracked branch
        back to ``base_head`` and hard-reset the work tree, so the change sits on a
        review branch and main is untouched. Caller holds the tree lock.
        """
        repo_path = str(self._path)
        tracked_ref = b"refs/heads/" + porcelain.active_branch(repo_path)
        side_ref = f"refs/heads/{branch}".encode()
        porcelain.update_ref(repo_path, side_ref, sha_str.encode())
        porcelain.update_ref(repo_path, tracked_ref, base_head.encode())
        porcelain.reset(repo_path, "hard", base_head.encode())

    def _push_review_branch(self, sha_str: str, branch: str, written: list[str]) -> PublishResult:
        """Push only the side branch a review commit was moved onto."""
        side_ref = f"refs/heads/{branch}".encode()
        pushed = False
        if self._pushes():
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
