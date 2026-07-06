#  Project:      dfe-engine
#  File:         tests/unit/test_gitops/test_dulwich_auth.py
#  Purpose:      Shared dulwich HTTPS cred embed + on-disk scrub
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""dulwich_auth: URL credential embed (HTTPS+token only) + remote scrub."""

from __future__ import annotations

from pathlib import Path

from dulwich import porcelain
from dulwich.repo import Repo

from dfe_engine.gitops.dulwich_auth import authed_https_url, scrub_remote_credentials


def test_embeds_username_and_token_on_https():
    out = authed_https_url("https://git.example.com/r.git", "ci-bot", "s3cr3t")
    assert out == "https://ci-bot:s3cr3t@git.example.com/r.git"


def test_embeds_on_plain_http_too():
    out = authed_https_url("http://git.local/r.git", "u", "t")
    assert out == "http://u:t@git.local/r.git"


def test_defaults_username_when_absent():
    out = authed_https_url("https://git.example.com/r.git", "", "tok")
    assert out == "https://x-access-token:tok@git.example.com/r.git"


def test_custom_default_username():
    out = authed_https_url("https://h/r.git", None, "tok", default_username="oauth2")
    assert out == "https://oauth2:tok@h/r.git"


def test_no_token_returns_url_unchanged():
    url = "https://git.example.com/r.git"
    assert authed_https_url(url, "ci-bot", "") == url
    assert authed_https_url(url, "ci-bot", None) == url


def test_ssh_url_passes_through_even_with_token():
    url = "git@github.com:org/repo.git"
    assert authed_https_url(url, "ci-bot", "tok") == url


def test_local_path_passes_through():
    url = "/srv/git/repo.git"
    assert authed_https_url(url, "ci-bot", "tok") == url


def test_scrub_rewrites_remote_to_bare_url(tmp_path: Path):
    """A clone whose remote carries username:token@ is scrubbed back to bare."""
    work = tmp_path / "clone"
    porcelain.init(str(work))
    bare = "https://git.example.com/deploy.git"
    with Repo(str(work)) as r:
        cfg = r.get_config()
        cfg.set((b"remote", b"origin"), b"url", b"https://ci-bot:s3cr3t@git.example.com/deploy.git")
        cfg.write_to_path()

    scrub_remote_credentials(work, bare)

    with Repo(str(work)) as r:
        stored = r.get_config().get((b"remote", b"origin"), b"url")
    assert stored == bare.encode()
    assert b"s3cr3t" not in (work / ".git" / "config").read_bytes()
