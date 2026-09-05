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

The same token must not survive on disk either: ``porcelain.clone`` persists the URL
it cloned from into ``.git/config``, so the clone is scrubbed back to the bare URL.
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
def logged_debug(monkeypatch):
    """Collect the debug lines the redacting stream emits, in order."""
    from dfe_engine.gitops import dulwich_auth

    lines: list[str] = []
    monkeypatch.setattr(dulwich_auth.logger, "debug", lines.append)
    return lines


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


def test_a_url_split_across_two_writes_is_still_redacted(logged_debug):
    # dulwich writes progress in whatever sizes the transport hands it, so the
    # userinfo can straddle a chunk boundary.
    head, tail = b"Push to http://dfe-admin:s3c", b"r3t@forge.svc/deploy.git successful.\n"
    stream = RedactingErrStream()
    assert stream.write(head) == len(head)
    assert stream.write(tail) == len(tail)
    stream.close()

    assert "s3cr3t" not in "".join(logged_debug)
    assert logged_debug == ["Push to http://***@forge.svc/deploy.git successful."]


def test_carriage_return_progress_is_split_and_redacted_line_by_line(logged_debug):
    """Sideband progress overwrites in place with \\r and only ends with \\n.

    Splitting on \\n alone buffered a whole fetch's progress into one blob that was
    redacted only at EOF, so a credentialed URL inside it sat unredacted meanwhile.
    """
    stream = RedactingErrStream()
    stream.write(
        b"Fetching http://dfe-admin:s3cr3t@forge.svc/deploy.git\r"
        b"Counting objects: 5\rCounting objects: 9, done.\n"
    )

    assert logged_debug == [
        "Fetching http://***@forge.svc/deploy.git",
        "Counting objects: 5",
        "Counting objects: 9, done.",
    ]


def test_a_crlf_line_does_not_log_an_empty_second_line(logged_debug):
    stream = RedactingErrStream()
    stream.write(b"Push to http://forge.svc/deploy.git successful.\r\n")
    stream.close()
    assert logged_debug == ["Push to http://forge.svc/deploy.git successful."]


def test_an_unterminated_line_is_flushed_redacted_on_close(logged_debug):
    stream = RedactingErrStream()
    stream.write(b"Push to http://dfe-admin:s3cr3t@forge.svc/deploy.git")
    assert logged_debug == []  # still buffered: no newline has arrived
    stream.close()
    assert logged_debug == ["Push to http://***@forge.svc/deploy.git"]


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


def test_the_clone_does_not_keep_the_token_in_git_config(tmp_path: Path, git_http_remote):
    """porcelain.clone persists the URL it cloned from; the scrub takes the creds back out.

    ``.git/config`` sits on the engine pod's config volume, so a token left there is
    readable by any co-located sidecar, exec shell or volume snapshot.
    """
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

    config = (work / ".git" / "config").read_text(encoding="utf-8")
    assert _TOKEN not in config
    assert f"{_USER}:" not in config
    assert url in config  # the bare URL is still the remote, so fetch/push resolve

    # A push re-supplies the credential per op, and must not write it back either.
    assert repo.publish({"deploy/values.yaml": "replicas: 1\n"}, message="publish me").pushed
    after = (work / ".git" / "config").read_text(encoding="utf-8")
    assert _TOKEN not in after


def test_reusing_an_existing_clone_rescrubs_the_stored_credentials(tmp_path: Path, git_http_remote):
    """A clone that died after writing .git/config would otherwise keep the token."""
    url, branch = git_http_remote
    work = tmp_path / "work"
    porcelain.clone(f"http://{_USER}:{_TOKEN}@{url.removeprefix('http://')}", str(work))
    assert _TOKEN in (work / ".git" / "config").read_text(encoding="utf-8")

    GitopsRepo(
        local_path=str(work),
        repo_url=url,
        branch=branch,
        username=_USER,
        token=_TOKEN,
    ).ensure()
    assert _TOKEN not in (work / ".git" / "config").read_text(encoding="utf-8")


def test_an_empty_token_still_scrubs_the_userinfo_off_disk(tmp_path: Path, git_http_remote):
    """The scrub guard has to match _authed_url's, which splices on the USERNAME.

    A username with no token still produced ``http://dfe-admin:@host``, and gating
    the scrub on the token instead left that userinfo in ``.git/config``.
    """
    url, branch = git_http_remote
    work = tmp_path / "work"
    porcelain.clone(f"http://{_USER}:@{url.removeprefix('http://')}", str(work))
    assert _USER in (work / ".git" / "config").read_text(encoding="utf-8")

    GitopsRepo(
        local_path=str(work),
        repo_url=url,
        branch=branch,
        username=_USER,
        token="",
    ).ensure()
    config = (work / ".git" / "config").read_text(encoding="utf-8")
    assert _USER not in config
    assert url in config  # the bare URL is still the remote, so fetch/push resolve


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
