#  Project:      dfe-engine
#  File:         tests/gitcrud/test_forge.py
#  Purpose:      Forge PR seam - real REST against a local HTTP server
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Forge providers open a real PR over HTTP (no mocks: a real local server).

A stdlib ``http.server`` on localhost stands in for Forgejo/GitHub/GitLab so the
provider's method, path, headers and JSON body are asserted against a real socket
round-trip, and the response is really parsed.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from dfe_engine.gitcrud.forge import (
    ForgeError,
    ForgejoForge,
    GitHubForge,
    GitLabForge,
    _split_repo_url,
    build_forge,
)
from dfe_engine.settings import GitopsSettings


@dataclass
class _Captured:
    method: str = ""
    path: str = ""
    headers: dict = field(default_factory=dict)
    body: dict = field(default_factory=dict)


@dataclass
class _Server:
    base_url: str
    captured: _Captured
    httpd: ThreadingHTTPServer

    def stop(self) -> None:
        self.httpd.shutdown()


def _start_server(status: int = 201, response: dict | None = None) -> _Server:
    captured = _Captured()
    payload = response if response is not None else {"number": 7, "html_url": "http://forge/pr/7"}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # silence
            return

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            captured.method = "POST"
            captured.path = self.path
            captured.headers = {k.lower(): v for k, v in self.headers.items()}
            captured.body = json.loads(raw or b"{}")
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    host, port = httpd.server_address
    return _Server(base_url=f"http://{host}:{port}", captured=captured, httpd=httpd)


@pytest.fixture
def server():
    srv = _start_server()
    yield srv
    srv.stop()


class TestForgejo:
    def test_open_pull_request_posts_and_parses(self, server):
        forge = ForgejoForge(api_base=server.base_url, owner="acme", repo="deploy", token="TKN")
        pr = forge.open_pull_request(
            head="dfe/helmvars/x/abc", base="main", title="cfg(x): set", body="why"
        )
        assert pr.number == 7
        assert pr.url == "http://forge/pr/7"
        assert pr.branch == "dfe/helmvars/x/abc"
        cap = server.captured
        assert cap.method == "POST"
        assert cap.path == "/api/v1/repos/acme/deploy/pulls"
        assert cap.headers["authorization"] == "token TKN"
        assert cap.body == {
            "head": "dfe/helmvars/x/abc",
            "base": "main",
            "title": "cfg(x): set",
            "body": "why",
        }

    def test_error_status_raises_forge_error(self):
        srv = _start_server(status=422, response={"message": "branch exists"})
        try:
            forge = ForgejoForge(api_base=srv.base_url, owner="a", repo="b", token="t")
            with pytest.raises(ForgeError):
                forge.open_pull_request(head="h", base="main", title="t", body="b")
        finally:
            srv.stop()


class TestGitHub:
    def test_github_path_and_bearer(self, server):
        forge = GitHubForge(api_base=server.base_url, owner="acme", repo="deploy", token="GH")
        pr = forge.open_pull_request(head="h", base="main", title="t", body="b")
        assert pr.number == 7
        cap = server.captured
        assert cap.path == "/repos/acme/deploy/pulls"
        assert cap.headers["authorization"] == "Bearer GH"
        assert cap.headers["accept"] == "application/vnd.github+json"
        assert cap.body["head"] == "h"


class TestGitLab:
    def test_gitlab_merge_request_shape(self):
        srv = _start_server(status=201, response={"iid": 3, "web_url": "http://gl/mr/3"})
        try:
            forge = GitLabForge(api_base=srv.base_url, owner="grp/sub", repo="deploy", token="GL")
            pr = forge.open_pull_request(head="h", base="main", title="t", body="desc")
            assert pr.number == 3
            assert pr.url == "http://gl/mr/3"
            cap = srv.captured
            # owner/repo is URL-encoded into the project id
            assert cap.path == "/api/v4/projects/grp%2Fsub%2Fdeploy/merge_requests"
            assert cap.headers["private-token"] == "GL"
            assert cap.body == {
                "source_branch": "h",
                "target_branch": "main",
                "title": "t",
                "description": "desc",
            }
        finally:
            srv.stop()


class TestSplitRepoUrl:
    @pytest.mark.parametrize(
        ("url", "host", "owner", "repo"),
        [
            ("https://github.com/acme/deploy.git", "github.com", "acme", "deploy"),
            ("https://git.example.com/acme/deploy", "git.example.com", "acme", "deploy"),
            ("git@github.com:acme/deploy.git", "github.com", "acme", "deploy"),
            ("ssh://git@git.example.com/acme/deploy.git", "git.example.com", "acme", "deploy"),
            ("https://gitlab.com/grp/sub/deploy.git", "gitlab.com", "grp/sub", "deploy"),
        ],
    )
    def test_shapes(self, url, host, owner, repo):
        c = _split_repo_url(url)
        assert (c.host, c.owner, c.repo) == (host, owner, repo)

    def test_bad_url_raises(self):
        with pytest.raises(ValueError):
            _split_repo_url("not-a-url")


class TestBuildForge:
    def _gs(self, **kw) -> GitopsSettings:
        base = {
            "enabled": True,
            "push": True,
            "repo_url": "https://git.example.com/acme/deploy.git",
        }
        base.update(kw)
        return GitopsSettings(**base)

    def test_infers_forgejo_for_selfhosted(self):
        forge = build_forge(self._gs())
        assert isinstance(forge, ForgejoForge)
        assert forge._base == "https://git.example.com"

    def test_infers_github(self):
        forge = build_forge(self._gs(repo_url="https://github.com/acme/deploy.git"))
        assert isinstance(forge, GitHubForge)
        assert forge._base == "https://api.github.com"

    def test_infers_gitlab(self):
        forge = build_forge(self._gs(repo_url="https://gitlab.com/acme/deploy.git"))
        assert isinstance(forge, GitLabForge)

    def test_provider_override_and_api_base(self):
        forge = build_forge(
            self._gs(forge_provider="github", forge_api_base="https://ghe.corp/api/v3")
        )
        assert isinstance(forge, GitHubForge)
        assert forge._base == "https://ghe.corp/api/v3"

    def test_none_when_push_off(self):
        assert build_forge(self._gs(push=False)) is None

    def test_none_when_no_repo_url(self):
        assert build_forge(self._gs(repo_url="")) is None

    def test_none_when_disabled(self):
        assert build_forge(self._gs(enabled=False)) is None

    def test_none_when_unparseable(self):
        assert build_forge(self._gs(repo_url="garbage")) is None
