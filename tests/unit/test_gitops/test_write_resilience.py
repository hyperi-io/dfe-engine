#  Project:      dfe-engine
#  File:         tests/unit/test_gitops/test_write_resilience.py
#  Purpose:      A deploy-repo write outlasts a slow forge and retries a transient one
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Real git over HTTP on loopback, with a forge that answers late or not at all.

The remote is dulwich's own smart-HTTP server behind a WSGI layer that finishes the
request and then holds the answer back, which is what a loaded forge does: the work
lands, the reply is slow. A write must wait longer than a read does, retry a
transient failure without committing twice, and give up with a typed error the API
answers 503.
"""

import socket
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from socketserver import ThreadingMixIn
from typing import Any
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

import pytest
from dulwich import porcelain
from dulwich.errors import NotGitRepository
from dulwich.object_store import tree_lookup_path
from dulwich.objects import Blob, Commit
from dulwich.repo import Repo
from dulwich.server import DictBackend
from dulwich.web import make_wsgi_chain
from prometheus_client.parser import text_string_to_metric_families
from scalo.metrics import create_metrics

from dfe_engine.gitops import repo as repo_module
from dfe_engine.gitops.metrics import WRITE_BREAKER, WRITE_RETRIES, GitopsMetrics
from dfe_engine.gitops.repo import GitopsRemoteError, GitopsRepo, GitopsUnavailableError
from dfe_engine.settings import GitopsWriteSettings

# Longer than the read bound, so a write held to it fails.
_SLOW_ADVERTISEMENT_SECONDS = repo_module.REMOTE_HEAD_TIMEOUT_SECONDS + 1.0

type Environ = dict[str, object]


class _QuietHandler(WSGIRequestHandler):
    """Request handler that does not narrate every request to stderr."""

    def log_message(self, format: str, *args: object) -> None:
        return None


class _ThreadingServer(ThreadingMixIn, WSGIServer):
    """One thread per request, so a held answer does not block the client's retry."""

    daemon_threads = True

    def handle_error(self, request: object, client_address: object) -> None:
        # The client gave up on a held answer and closed its end; that is the test.
        return None


class _HoldAnswers:
    """WSGI layer that runs the request to completion, then holds its answer back.

    The git server writes through the legacy ``write`` callable, so the whole
    response is buffered before the real ``start_response`` is called.
    """

    def __init__(
        self, app: Callable, *, when: Callable[[Environ], bool], seconds: float, times: int
    ):
        self._app = app
        self._when = when
        self._seconds = seconds
        self._left = times
        self._lock = threading.Lock()
        self.held = 0

    def _take(self, environ: Environ) -> bool:
        with self._lock:
            if self._left <= 0 or not self._when(environ):
                return False
            self._left -= 1
            self.held += 1
            return True

    def __call__(self, environ: Environ, start_response: Callable) -> list[bytes]:
        if not self._take(environ):
            return self._app(environ, start_response)
        started: list[tuple] = []
        chunks: list[bytes] = []

        def buffer_start(status: str, headers: list, exc_info: object = None) -> Callable:
            started.append((status, headers))
            return chunks.append

        chunks.extend(self._app(environ, buffer_start))
        time.sleep(self._seconds)
        start_response(*started[0])
        return chunks


def _is_fetch_advertisement(environ: Environ) -> bool:
    return environ.get("QUERY_STRING") == "service=git-upload-pack"


def _is_push_upload(environ: Environ) -> bool:
    return environ.get("REQUEST_METHOD") == "POST" and str(environ.get("PATH_INFO", "")).endswith(
        "/git-receive-pack"
    )


def _seed(tmp_path: Path) -> tuple[Path, str]:
    """A bare deploy repo with one commit, and the branch it lives on."""
    bare = tmp_path / "remote.git"
    porcelain.init(str(bare), bare=True)
    seed = tmp_path / "seed"
    porcelain.init(str(seed))
    (seed / "README").write_text("seed\n", encoding="utf-8")
    porcelain.add(str(seed), paths=[str(seed / "README")])
    porcelain.commit(str(seed), message=b"init", author=b"t <t@t>", committer=b"t <t@t>")
    branch = porcelain.active_branch(str(seed)).decode()
    porcelain.push(str(seed), str(bare), f"refs/heads/{branch}".encode())
    return bare, branch


@pytest.fixture
def no_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Loopback must not go through a proxy the CI host happens to export."""
    for name in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
        monkeypatch.delenv(name, raising=False)


type Hold = Callable[[Callable], _HoldAnswers]


@pytest.fixture
def serve(no_proxy: None) -> Iterator[Callable[..., str]]:
    """Start a git-over-HTTP server for a bare repo, optionally holding some answers."""
    servers: list[tuple[WSGIServer, threading.Thread]] = []

    def start(bare: Path, hold: Hold | None = None) -> str:
        app = make_wsgi_chain(DictBackend({"/": Repo(str(bare))}))
        if hold is not None:
            app = hold(app)
        server = make_server(
            "127.0.0.1", 0, app, server_class=_ThreadingServer, handler_class=_QuietHandler
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append((server, thread))
        return f"http://127.0.0.1:{server.server_port}/"

    yield start
    for server, thread in servers:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _clone(
    tmp_path: Path,
    bare: Path,
    branch: str,
    url: str,
    *,
    metrics: GitopsMetrics | None = None,
    write: GitopsWriteSettings | None = None,
) -> GitopsRepo:
    """A pushing clone of ``bare``, taken over the local path and then pointed at ``url``.

    Cloning over the local path keeps the server's held answers for the write under test.
    """
    work = tmp_path / "work"
    porcelain.clone(str(bare), str(work), branch=branch.encode())
    return GitopsRepo(
        local_path=str(work), repo_url=url, branch=branch, push=True, metrics=metrics, write=write
    )


def _manager():
    return create_metrics("test", backend="prometheus", enable_auto_update=False)


def _sample(manager, name: str, labels: dict[str, str]) -> float:
    """One counter sample, read off the real exposition."""
    for family in text_string_to_metric_families(manager.metrics_text):
        for sample in family.samples:
            if sample.name == name and all(sample.labels.get(k) == v for k, v in labels.items()):
                return sample.value
    return 0.0


def _retries(manager, op: str, outcome: str) -> float:
    """The write-retry counter for one call and outcome."""
    return _sample(manager, WRITE_RETRIES, {"op": op, "outcome": outcome})


def _breaker(manager, event: str) -> float:
    """The write-breaker counter for one event."""
    return _sample(manager, WRITE_BREAKER, {"event": event})


def _head(bare: Path, branch: str) -> bytes:
    with Repo(str(bare)) as repo:
        return repo.refs[f"refs/heads/{branch}".encode()]


def test_a_write_waits_past_the_read_bound_for_a_slow_forge(tmp_path: Path, serve) -> None:
    """The forge answers the fetch after the read bound: the write still lands.

    Held to the read bound, this fails with the read timeout a loaded forge produced
    on a cold cluster, and the API answered it 500.
    """
    bare, branch = _seed(tmp_path)
    holder: list[_HoldAnswers] = []

    def hold(app: Callable) -> _HoldAnswers:
        holder.append(
            _HoldAnswers(
                app,
                when=_is_fetch_advertisement,
                seconds=_SLOW_ADVERTISEMENT_SECONDS,
                times=1,
            )
        )
        return holder[0]

    url = serve(bare, hold)
    repo = _clone(tmp_path, bare, branch, url)

    result = repo.publish({"values/a.yaml": "a: 1\n"}, message="slow forge")

    assert holder[0].held == 1
    assert result.pushed is True
    assert _head(bare, branch).decode() == result.commit_sha


def test_a_push_whose_answer_is_lost_is_not_committed_twice(tmp_path: Path, serve) -> None:
    """The forge takes the push but answers after the client gave up: one commit lands.

    The retry pushes the same commit, finds the ref already there and sends nothing.
    """
    bare, branch = _seed(tmp_path)
    seed_head = _head(bare, branch)
    holder: list[_HoldAnswers] = []

    def hold(app: Callable) -> _HoldAnswers:
        holder.append(_HoldAnswers(app, when=_is_push_upload, seconds=3.0, times=1))
        return holder[0]

    url = serve(bare, hold)
    manager = _manager()
    repo = _clone(
        tmp_path,
        bare,
        branch,
        url,
        metrics=GitopsMetrics(manager),
        write=GitopsWriteSettings(
            timeout_seconds=1.0, wait_initial=0.05, wait_max=0.1, budget_seconds=5.0
        ),
    )

    result = repo.publish({"values/a.yaml": "a: 1\n"}, message="lost answer")

    assert holder[0].held == 1
    assert result.pushed is True
    head = _head(bare, branch)
    assert head.decode() == result.commit_sha
    with Repo(str(bare)) as remote:
        commit = remote[head]
        assert isinstance(commit, Commit)
        assert commit.parents == [seed_head]
    assert _retries(manager, "push", "retried") == 1
    assert _retries(manager, "push", "exhausted") == 0


def test_an_unreachable_forge_exhausts_the_budget_and_is_counted(tmp_path: Path, no_proxy) -> None:
    """Nothing listens: every attempt is refused, the write gives up typed and counted."""
    bare, branch = _seed(tmp_path)
    seed_head = _head(bare, branch)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    manager = _manager()
    repo = _clone(
        tmp_path,
        bare,
        branch,
        f"http://127.0.0.1:{port}/deploy.git",
        metrics=GitopsMetrics(manager),
        write=GitopsWriteSettings(
            timeout_seconds=1.0, wait_initial=0.01, wait_max=0.02, budget_seconds=0.2
        ),
    )

    with pytest.raises(GitopsUnavailableError) as caught:
        repo.publish({"values/a.yaml": "a: 1\n"}, message="nowhere")

    assert caught.value.remote == f"http://127.0.0.1:{port}/deploy.git"
    assert caught.value.retry_after_seconds >= 1
    assert "gitops deploy repo" in str(caught.value)
    assert _retries(manager, "fetch", "retried") >= 1
    assert _retries(manager, "fetch", "exhausted") == 1
    assert _head(bare, branch) == seed_head


def test_a_write_gives_up_on_a_forge_that_never_answers(tmp_path: Path, no_proxy) -> None:
    """A forge that accepts the connection and goes silent costs one write timeout."""
    bare, branch = _seed(tmp_path)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    try:
        repo = _clone(
            tmp_path,
            bare,
            branch,
            f"http://127.0.0.1:{listener.getsockname()[1]}/deploy.git",
            write=GitopsWriteSettings(timeout_seconds=1.0, budget_seconds=0.0),
        )
        started = time.monotonic()
        with pytest.raises(GitopsUnavailableError):
            repo.publish({"values/a.yaml": "a: 1\n"}, message="silent forge")
        assert time.monotonic() - started < 10.0
    finally:
        listener.close()


def test_a_failure_another_attempt_cannot_fix_is_not_retried(tmp_path: Path, serve) -> None:
    """The forge has no such repo: the write fails at once, not after the budget."""
    bare, branch = _seed(tmp_path)
    url = serve(bare)
    manager = _manager()
    repo = _clone(
        tmp_path,
        bare,
        branch,
        url + "missing.git/",
        metrics=GitopsMetrics(manager),
        write=GitopsWriteSettings(timeout_seconds=1.0, budget_seconds=30.0),
    )

    started = time.monotonic()
    with pytest.raises(GitopsRemoteError) as caught:
        repo.publish({"values/a.yaml": "a: 1\n"}, message="no repo")

    assert not isinstance(caught.value, GitopsUnavailableError)
    assert caught.value.transient is False
    assert time.monotonic() - started < 10.0
    assert _retries(manager, "fetch", "retried") == 0


def _remote_file(bare: Path, branch: str, rel: str) -> str:
    """A file's content at the remote branch's head."""
    with Repo(str(bare)) as repo:
        commit = repo[repo.refs[f"refs/heads/{branch}".encode()]]
        assert isinstance(commit, Commit)
        _mode, sha = tree_lookup_path(repo.get_object, commit.tree, rel.encode())
        blob = repo[sha]
        assert isinstance(blob, Blob)
        return blob.data.decode()


def test_a_push_that_landed_is_not_re_applied_over_a_peers_write(tmp_path: Path, monkeypatch):
    """Our push lands, its answer is lost, and a peer writes the same file on top.

    Re-applying our older content on the peer's head would revert the peer's write
    while both writers were told they succeeded.
    """
    bare, branch = _seed(tmp_path)
    ours = GitopsRepo(
        local_path=str(tmp_path / "ours"), repo_url=str(bare), branch=branch, push=True
    )
    peer = GitopsRepo(
        local_path=str(tmp_path / "peer"), repo_url=str(bare), branch=branch, push=True
    )
    ours.ensure()
    peer.ensure()

    real_push = porcelain.push
    lost: list[bool] = []

    def lose_our_first_answer(path: Any, *args: Any, **kwargs: Any) -> Any:
        result = real_push(path, *args, **kwargs)
        if str(path) == str(tmp_path / "ours") and not lost:
            lost.append(True)
            peer.publish({"x.yaml": "v: 2\n"}, message="peer, newer")
            raise TimeoutError("read timed out after the forge applied the push")
        return result

    monkeypatch.setattr(repo_module.porcelain, "push", lose_our_first_answer)
    result = ours.publish({"x.yaml": "v: 1\n"}, message="ours, older")

    assert lost == [True]
    assert result.pushed is True
    assert _remote_file(bare, branch, "x.yaml") == "v: 2\n"
    with Repo(str(bare)) as remote:
        head = remote[remote.refs[f"refs/heads/{branch}".encode()]]
        assert isinstance(head, Commit)
        assert head.parents == [(result.commit_sha or "").encode()]
    assert ours.head_revision() == head.id.decode()


def _faked_fetch_clone(tmp_path: Path) -> GitopsRepo:
    """A pushing clone whose fetch the test replaces, so the URL is never dialled."""
    bare, branch = _seed(tmp_path)
    return _clone(
        tmp_path,
        bare,
        branch,
        "http://127.0.0.1:9/deploy.git",
        write=GitopsWriteSettings(
            timeout_seconds=1.0, wait_initial=0.01, wait_max=0.02, budget_seconds=2.0
        ),
    )


def test_a_hard_failure_on_a_retry_is_not_retried(tmp_path: Path, monkeypatch) -> None:
    """The first fetch times out and the second finds no repo: fail at once, not 503.

    A retry runs inside the handler for the first failure, so its error carries that
    timeout as context; read as its cause, it made the missing repo look transient.
    """
    repo = _faked_fetch_clone(tmp_path)
    timeouts: list[float] = []

    def fetch(_errstream: object, *, timeout: float) -> object:
        timeouts.append(timeout)
        if len(timeouts) == 1:
            raise TimeoutError("the first fetch timed out")
        raise NotGitRepository("no such repo")

    monkeypatch.setattr(repo, "_fetch", fetch)
    with pytest.raises(GitopsRemoteError) as caught:
        repo.publish({"x.yaml": "v: 1\n"}, message="gone")

    assert not isinstance(caught.value, GitopsUnavailableError)
    assert caught.value.transient is False
    assert len(timeouts) == 2


def test_a_callers_own_timeout_does_not_make_a_hard_failure_transient(
    tmp_path: Path, monkeypatch
) -> None:
    """publish called while its caller handles a timeout: a missing repo fails at once."""
    repo = _faked_fetch_clone(tmp_path)
    timeouts: list[float] = []

    def fetch(_errstream: object, *, timeout: float) -> object:
        timeouts.append(timeout)
        raise NotGitRepository("no such repo")

    monkeypatch.setattr(repo, "_fetch", fetch)
    try:
        raise TimeoutError("the caller's own timeout")
    except TimeoutError:
        with pytest.raises(GitopsRemoteError) as caught:
            repo.publish({"x.yaml": "v: 1\n"}, message="gone")

    assert not isinstance(caught.value, GitopsUnavailableError)
    assert caught.value.transient is False
    assert len(timeouts) == 1


def test_the_budget_bounds_a_call_to_a_silent_forge(tmp_path: Path, no_proxy) -> None:
    """A retry waits only for what is left of the budget, so the budget ends the call.

    With every retry waiting its full timeout, a forge that accepts and goes silent
    cost two timeouts plus the budget.
    """
    timeout, budget = 2.0, 0.5
    bare, branch = _seed(tmp_path)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(16)
    try:
        repo = _clone(
            tmp_path,
            bare,
            branch,
            f"http://127.0.0.1:{listener.getsockname()[1]}/deploy.git",
            write=GitopsWriteSettings(
                timeout_seconds=timeout, wait_initial=0.05, wait_max=0.1, budget_seconds=budget
            ),
        )
        started = time.monotonic()
        with pytest.raises(GitopsUnavailableError):
            repo.publish({"x.yaml": "v: 1\n"}, message="silent forge")
        elapsed = time.monotonic() - started
    finally:
        listener.close()

    assert elapsed < timeout + budget + 0.5


class _CountingBlackHole:
    """A listener that takes every connection and never answers on it, counting each.

    The wedged-forge shape from the black-holed forge in test_repo.py, with the
    connections accepted so a test can tell whether a write dialled at all.
    """

    def __init__(self) -> None:
        self._listener = socket.socket()
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(16)
        self._held: list[socket.socket] = []
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._accept, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._listener.getsockname()[1]}/deploy.git"

    @property
    def connections(self) -> int:
        with self._lock:
            return len(self._held)

    def _accept(self) -> None:
        while True:
            try:
                conn, _addr = self._listener.accept()
            except OSError:
                return
            with self._lock:
                self._held.append(conn)

    def close(self) -> None:
        self._listener.close()
        self._thread.join(timeout=5)
        with self._lock:
            for conn in self._held:
                conn.close()


@pytest.fixture
def black_hole(no_proxy: None) -> Iterator[_CountingBlackHole]:
    hole = _CountingBlackHole()
    try:
        yield hole
    finally:
        hole.close()


def test_the_breaker_opens_after_spent_budgets_and_answers_without_dialling(
    tmp_path: Path, black_hole: _CountingBlackHole
) -> None:
    """Two writes spend their budget on a wedged forge; the third is a 503 at once.

    Without the breaker every write against a forge that is down for minutes costs
    its full timeout and budget, with the writer lock held and later writes queued.
    """
    timeout, reset = 0.5, 60.0
    bare, branch = _seed(tmp_path)
    manager = _manager()
    repo = _clone(
        tmp_path,
        bare,
        branch,
        black_hole.url,
        metrics=GitopsMetrics(manager),
        write=GitopsWriteSettings(
            timeout_seconds=timeout, budget_seconds=0.0, failure_threshold=2, reset_timeout=reset
        ),
    )

    for _ in range(2):
        with pytest.raises(GitopsUnavailableError):
            repo.publish({"x.yaml": "v: 1\n"}, message="wedged forge")
    dialled = black_hole.connections
    assert dialled >= 2
    assert _breaker(manager, "opened") == 1

    started = time.monotonic()
    with pytest.raises(GitopsUnavailableError) as caught:
        repo.publish({"x.yaml": "v: 1\n"}, message="breaker open")
    elapsed = time.monotonic() - started

    assert elapsed < timeout
    assert black_hole.connections == dialled
    assert caught.value.remote == black_hole.url
    assert reset - 5 <= caught.value.retry_after_seconds <= reset
    assert "gitops deploy repo" in str(caught.value)
    assert _breaker(manager, "rejected") == 1
    assert _retries(manager, "fetch", "exhausted") == 2


def test_the_write_that_opens_the_breaker_says_when_to_come_back(
    tmp_path: Path, no_proxy: None
) -> None:
    """Retry-After on the write that trips it is the breaker's window, not one back-off."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    bare, branch = _seed(tmp_path)
    repo = _clone(
        tmp_path,
        bare,
        branch,
        f"http://127.0.0.1:{port}/deploy.git",
        write=GitopsWriteSettings(budget_seconds=0.0, failure_threshold=1, reset_timeout=45.0),
    )

    with pytest.raises(GitopsUnavailableError) as caught:
        repo.publish({"x.yaml": "v: 1\n"}, message="refused")

    assert caught.value.retry_after_seconds >= 40


class _Outage:
    """WSGI layer in front of the real git server: a forge that is down, or answering.

    ``answer`` is None to pass requests through, or the status every request gets
    instead -- a 503 is a forge restarting behind its proxy, a 404 a forge that is
    up and has no such repo. Every request is counted, served or not.
    """

    def __init__(self, app: Callable) -> None:
        self._app = app
        self._lock = threading.Lock()
        self.answer: str | None = "503 Service Unavailable"
        self.requests = 0

    def __call__(self, environ: Environ, start_response: Callable) -> Any:
        with self._lock:
            self.requests += 1
            answer = self.answer
        if answer is None:
            return self._app(environ, start_response)
        start_response(answer, [("Content-Type", "text/plain")])
        return [b"not now\n"]


def _outage_clone(
    tmp_path: Path, serve, manager, *, reset: float
) -> tuple[GitopsRepo, _Outage, Path, str]:
    """A pushing clone of a real forge that starts out down."""
    bare, branch = _seed(tmp_path)
    outages: list[_Outage] = []

    def wrap(app: Callable) -> _Outage:
        outages.append(_Outage(app))
        return outages[0]

    url = serve(bare, wrap)
    repo = _clone(
        tmp_path,
        bare,
        branch,
        url,
        metrics=GitopsMetrics(manager),
        write=GitopsWriteSettings(
            timeout_seconds=2.0, budget_seconds=0.0, failure_threshold=2, reset_timeout=reset
        ),
    )
    return repo, outages[0], bare, branch


def _open_the_breaker(repo: GitopsRepo, outage: _Outage) -> None:
    for _ in range(2):
        with pytest.raises(GitopsUnavailableError):
            repo.publish({"x.yaml": "v: 0\n"}, message="forge down")
    requests = outage.requests
    with pytest.raises(GitopsUnavailableError):
        repo.publish({"x.yaml": "v: 0\n"}, message="breaker open")
    assert outage.requests == requests


def test_a_probe_the_forge_answers_closes_the_breaker(tmp_path: Path, serve) -> None:
    """The forge comes back: the first write after the window lands, and so do the rest."""
    reset = 0.3
    manager = _manager()
    repo, outage, bare, branch = _outage_clone(tmp_path, serve, manager, reset=reset)
    _open_the_breaker(repo, outage)

    outage.answer = None
    time.sleep(reset + 0.1)
    probe = repo.publish({"x.yaml": "v: 1\n"}, message="probe")

    assert probe.pushed is True
    assert _head(bare, branch).decode() == probe.commit_sha
    assert _breaker(manager, "closed") == 1

    requests = outage.requests
    after = repo.publish({"y.yaml": "v: 1\n"}, message="after the probe")
    assert after.pushed is True
    assert outage.requests > requests
    assert _breaker(manager, "rejected") == 1


def test_a_probe_that_spends_its_budget_reopens_the_breaker(tmp_path: Path, serve) -> None:
    """The forge is still down at the probe: the breaker opens again for a new window."""
    reset = 0.3
    manager = _manager()
    repo, outage, _bare, _branch = _outage_clone(tmp_path, serve, manager, reset=reset)
    _open_the_breaker(repo, outage)

    time.sleep(reset + 0.1)
    requests = outage.requests
    with pytest.raises(GitopsUnavailableError):
        repo.publish({"x.yaml": "v: 1\n"}, message="probe")
    assert outage.requests > requests
    assert _breaker(manager, "opened") == 2

    requests = outage.requests
    with pytest.raises(GitopsUnavailableError):
        repo.publish({"x.yaml": "v: 1\n"}, message="reopened")
    assert outage.requests == requests
    assert _breaker(manager, "closed") == 0


def test_a_forge_that_answers_no_never_opens_the_breaker(tmp_path: Path, serve) -> None:
    """A missing repo is the forge up and answering: every write reports it, none is a 503."""
    manager = _manager()
    repo, outage, _bare, _branch = _outage_clone(tmp_path, serve, manager, reset=60.0)
    outage.answer = "404 Not Found"

    for _ in range(4):
        requests = outage.requests
        with pytest.raises(GitopsRemoteError) as caught:
            repo.publish({"x.yaml": "v: 1\n"}, message="no such repo")
        assert not isinstance(caught.value, GitopsUnavailableError)
        assert outage.requests > requests
    assert _breaker(manager, "opened") == 0


def test_a_probe_the_forge_refuses_closes_the_breaker_and_reports_the_refusal(
    tmp_path: Path, serve
) -> None:
    """Back up but answering 404: the probe surfaces the real error, not another 503."""
    reset = 0.3
    manager = _manager()
    repo, outage, _bare, _branch = _outage_clone(tmp_path, serve, manager, reset=reset)
    _open_the_breaker(repo, outage)

    outage.answer = "404 Not Found"
    time.sleep(reset + 0.1)
    with pytest.raises(GitopsRemoteError) as caught:
        repo.publish({"x.yaml": "v: 1\n"}, message="probe")

    assert not isinstance(caught.value, GitopsUnavailableError)
    assert _breaker(manager, "closed") == 1
