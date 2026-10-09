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
from tests.support.loopback import stop_server


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
    thread: threading.Thread

    def stop(self) -> None:
        stop_server(self.httpd, self.thread)


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
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address
    return _Server(base_url=f"http://{host}:{port}", captured=captured, httpd=httpd, thread=thread)


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

    def test_http_port_is_kept_in_the_authority(self):
        # A self-hosted forge on a non-default port is the DFE DEFAULT (Forgejo
        # :3000), and the API answers on the same port as the git remote.
        c = _split_repo_url(
            "http://dfe-forgejo.forgejo.svc.cluster.local:3000/dfe-admin/deploy.git"
        )
        assert c.authority == "dfe-forgejo.forgejo.svc.cluster.local:3000"
        # host stays bare so provider inference / the github.com check never see a port.
        assert c.host == "dfe-forgejo.forgejo.svc.cluster.local"

    def test_authority_matches_host_when_no_port(self):
        c = _split_repo_url("https://git.example.com/acme/deploy.git")
        assert c.authority == c.host == "git.example.com"

    def test_ssh_port_is_not_carried_into_the_api_authority(self):
        # ssh://host:2222 is the SSH port -- the REST API is not there, so carrying
        # it would swap one unreachable api_base for another.
        c = _split_repo_url("ssh://git@git.example.com:2222/acme/deploy.git")
        assert c.authority == "git.example.com"


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

    def test_selfhosted_api_base_keeps_the_remote_port(self):
        # REGRESSION (live 2026-07-17): the api_base was built from urlsplit().hostname,
        # which drops the port, so the in-cluster Forgejo remote became
        # http://dfe-forgejo...  -> :80, nothing listening. The PR POST hung for the
        # full 15s client timeout and surfaced as "forgejo PR request error: timed out",
        # i.e. review PRs could never work on ANY self-hosted forge on a non-default
        # port. Worse, the blocking call sat in an async endpoint, so it starved the
        # event loop until the health probes failed and the engine was killed.
        forge = build_forge(
            self._gs(
                repo_url="http://dfe-forgejo.forgejo.svc.cluster.local:3000/dfe-admin/deploy.git"
            )
        )
        assert isinstance(forge, ForgejoForge)
        assert forge._base == "http://dfe-forgejo.forgejo.svc.cluster.local:3000"

    def test_selfhosted_github_api_base_keeps_the_remote_port(self):
        # GitHub Enterprise derives /api/v3 from the remote, so it needs the port too.
        # provider is explicit: "ghe.corp" carries no "github" for _infer_provider.
        forge = build_forge(
            self._gs(forge_provider="github", repo_url="https://ghe.corp:8443/acme/deploy.git")
        )
        assert isinstance(forge, GitHubForge)
        assert forge._base == "https://ghe.corp:8443/api/v3"

    def test_none_when_push_off(self):
        assert build_forge(self._gs(push=False)) is None

    def test_none_when_no_repo_url(self):
        assert build_forge(self._gs(repo_url="")) is None

    def test_none_when_disabled(self):
        assert build_forge(self._gs(enabled=False)) is None

    def test_none_when_unparseable(self):
        assert build_forge(self._gs(repo_url="garbage")) is None
