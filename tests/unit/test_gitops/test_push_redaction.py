#  Project:      dfe-engine
#  File:         tests/unit/test_gitops/test_push_redaction.py
#  Purpose:      The deploy-repo token never reaches a log or an error message
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A gitops push must not print the deploy-repo token.

``dulwich.porcelain.push`` writes ``Push to <remote_location> successful.`` to its
``errstream`` (stderr by default) and puts the same URL in its failure message, and
``remote_location`` is the URL AS SUPPLIED. HTTPS auth has to be carried in that URL
(porcelain takes no credential kwargs), so every publish used to log
``Push to http://dfe-admin:<token>@forge/deploy.git successful.`` in clear.

Real git throughout: a dulwich WSGI git server on loopback is the remote, the push
is a real HTTPS-shaped push with credentials in the URL, and the assertion reads
file-descriptor-level captured output - so it sees whatever ANY layer writes to
stderr, not just what this code chose to log.
"""

from __future__ import annotations

import threading
from pathlib import Path
from wsgiref.simple_server import WSGIRequestHandler, make_server

import pytest
from dulwich import porcelain
from dulwich.repo import Repo
from dulwich.server import DictBackend
from dulwich.web import make_wsgi_chain

from dfe_engine.gitops.dulwich_auth import RedactingErrStream, redact_credentials
from dfe_engine.gitops.repo import GitopsRemoteError, GitopsRepo

_TOKEN = "s3cr3t-deploy-token"
_USER = "dfe-admin"


class _QuietHandler(WSGIRequestHandler):
    """Request handler that does not narrate to stderr (the stream under test)."""

    def log_message(self, format: str, *args) -> None:
        return None


@pytest.fixture
def git_http_remote(tmp_path: Path):
    """A real git-over-HTTP remote on loopback, seeded with one commit.

    Yields ``(url, branch)``. The seed goes in over the local path so the fixture
    itself never pushes over HTTP - only the code under test does.
    """
    bare = tmp_path / "remote.git"
    porcelain.init(str(bare), bare=True)

    seed = tmp_path / "seed"
    porcelain.init(str(seed))
    (seed / "README").write_text("seed\n", encoding="utf-8")
    porcelain.add(str(seed), paths=[str(seed / "README")])
    porcelain.commit(str(seed), message=b"init", author=b"t <t@t>", committer=b"t <t@t>")
    branch = porcelain.active_branch(str(seed)).decode()
    porcelain.push(str(seed), str(bare), f"refs/heads/{branch}".encode())

    app = make_wsgi_chain(DictBackend({"/": Repo(str(bare))}))
    server = make_server("127.0.0.1", 0, app, handler_class=_QuietHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/", branch
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_the_dulwich_success_line_loses_its_credentials():
    line = "Push to http://dfe-admin:s3cr3t@forge.svc:3000/dfe-admin/deploy.git successful."
    redacted = redact_credentials(line)
    assert "s3cr3t" not in redacted
    assert redacted.startswith("Push to http://***@forge.svc:3000/dfe-admin/deploy.git")


def test_a_credential_free_url_is_left_alone():
    for url in ("https://github.com/hyperi-io/dfe-engine.git", "ssh://git@forge/deploy.git"):
        assert redact_credentials(url) == url


def test_the_errstream_never_puts_the_token_on_stderr(capfd):
    stream = RedactingErrStream()
    written = stream.write(
        b"Push to http://dfe-admin:s3cr3t@forge.svc:3000/dfe-admin/deploy.git successful.\n"
    )
    assert written > 0  # dulwich checks the byte count it wrote
    captured = capfd.readouterr()
    assert "s3cr3t" not in captured.out + captured.err


def test_a_real_credentialed_push_keeps_the_token_out_of_every_stream(
    tmp_path: Path, git_http_remote, capfd
):
    url, branch = git_http_remote
    work = tmp_path / "work"
    repo = GitopsRepo(
        local_path=str(work),
        repo_url=url,
        branch=branch,
        push=True,
        username=_USER,
        token=_TOKEN,
    )
    repo.ensure()
    result = repo.publish({"deploy/values.yaml": "replicas: 1\n"}, message="publish me")
    assert result.pushed is True

    check = tmp_path / "check"
    porcelain.clone(url, str(check))
    assert (check / "deploy" / "values.yaml").read_text() == "replicas: 1\n"

    captured = capfd.readouterr()
    assert _TOKEN not in captured.out + captured.err


def test_a_failed_credentialed_push_keeps_the_token_out_of_the_error(tmp_path: Path, capfd):
    work = tmp_path / "work"
    porcelain.init(str(work))
    (work / "a.yaml").write_text("a: 1\n", encoding="utf-8")
    porcelain.add(str(work), paths=[str(work / "a.yaml")])
    porcelain.commit(str(work), message=b"init", author=b"t <t@t>", committer=b"t <t@t>")

    # Port 1 is never listening, so the push fails inside dulwich with the URL to hand.
    repo = GitopsRepo(
        local_path=str(work),
        repo_url="http://127.0.0.1:1/dfe-admin/deploy.git",
        branch=porcelain.active_branch(str(work)).decode(),
        push=True,
        username=_USER,
        token=_TOKEN,
    )
    with pytest.raises(GitopsRemoteError) as caught:
        repo.publish({"b.yaml": "b: 2\n"}, message="will not land")

    assert _TOKEN not in str(caught.value)
    captured = capfd.readouterr()
    assert _TOKEN not in captured.out + captured.err
