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
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

import pytest
from dulwich import porcelain
from dulwich.objects import Commit
from dulwich.repo import Repo
from dulwich.server import DictBackend
from dulwich.web import make_wsgi_chain
from prometheus_client.parser import text_string_to_metric_families
from scalo.metrics import create_metrics

from dfe_engine.gitops import repo as repo_module
from dfe_engine.gitops.metrics import WRITE_RETRIES, GitopsMetrics
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


def _retries(manager, op: str, outcome: str) -> float:
    """The write-retry counter for one call and outcome, read off the real exposition."""
    for family in text_string_to_metric_families(manager.metrics_text):
        for sample in family.samples:
            if (
                sample.name == WRITE_RETRIES
                and sample.labels.get("op") == op
                and sample.labels.get("outcome") == outcome
            ):
                return sample.value
    return 0.0


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
