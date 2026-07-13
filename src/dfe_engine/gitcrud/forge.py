#  Project:      dfe-engine
#  File:         gitcrud/forge.py
#  Purpose:      Provider-agnostic seam to open a review PR on the deploy repo
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Open a pull/merge request on the deploy repo's git forge.

When a production+team governed write may NOT commit straight to main (see
gitcrud/routing.py + docs/control-plane/gitops-commit-standard.md section 4), the engine pushes
a short-lived branch and opens a PR for review. The forge REST call is a seam:
Forgejo/Gitea, GitHub and GitLab plug in behind one ``ForgeProvider`` protocol,
selected from settings. REST goes over scalo's ``HttpClient`` (retries +
raise_for_status baked in) -- never a git CLI, matching the rest of the gitops
bridge.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol
from urllib.parse import quote, urlsplit

import httpx
from scalo.http import HttpClient
from scalo.logger import logger

if TYPE_CHECKING:
    from dfe_engine.settings import GitopsSettings


@dataclass(frozen=True)
class PullRequest:
    """A freshly opened review PR (or merge request)."""

    number: int
    url: str
    branch: str


class ForgeError(RuntimeError):
    """Raised when the forge refuses or fails to open the PR."""


class ForgeProvider(Protocol):
    """Anything that can open a review PR for a pushed branch."""

    def open_pull_request(self, *, head: str, base: str, title: str, body: str) -> PullRequest: ...


@dataclass(frozen=True)
class _RepoCoords:
    scheme: str
    host: str
    owner: str
    repo: str


def _split_repo_url(repo_url: str) -> _RepoCoords:
    """Parse ``owner`` + ``repo`` (and host) from an HTTPS or SSH remote URL.

    Handles ``https://host/owner/repo(.git)`` and the two SSH shapes
    ``git@host:owner/repo(.git)`` and ``ssh://git@host/owner/repo(.git)``.
    """
    url = repo_url.strip()
    if url.startswith(("http://", "https://", "ssh://")):
        parts = urlsplit(url)
        scheme = "https" if parts.scheme in ("ssh", "") else parts.scheme
        host = parts.hostname or ""
        path = parts.path
    elif "@" in url and ":" in url.split("@", 1)[1]:
        # scp-like SSH: git@host:owner/repo.git
        host_part, path = url.split("@", 1)[1].split(":", 1)
        host = host_part
        scheme = "https"
    else:
        raise ValueError(f"unrecognised repo_url: {repo_url!r}")

    segments = [s for s in path.strip("/").split("/") if s]
    if len(segments) < 2:
        raise ValueError(f"repo_url has no owner/repo: {repo_url!r}")
    owner = "/".join(segments[:-1])  # GitLab allows nested groups
    repo = segments[-1]
    if repo.endswith(".git"):
        repo = repo[:-4]
    if not host or not owner or not repo:
        raise ValueError(f"repo_url missing host/owner/repo: {repo_url!r}")
    return _RepoCoords(scheme=scheme, host=host, owner=owner, repo=repo)


def _infer_provider(host: str) -> str:
    h = host.lower()
    if "github" in h:
        return "github"
    if "gitlab" in h:
        return "gitlab"
    return "forgejo"  # Forgejo/Gitea is the self-hosted default


class _RestForge:
    """Shared REST plumbing: one POST, wrap failures as ForgeError."""

    provider = "rest"

    def __init__(
        self,
        *,
        api_base: str,
        owner: str,
        repo: str,
        token: str = "",
        client: HttpClient | None = None,
    ) -> None:
        self._base = api_base.rstrip("/")
        self._owner = owner
        self._repo = repo
        self._token = token
        self._client = client

    def open_pull_request(self, *, head: str, base: str, title: str, body: str) -> PullRequest:
        """Each provider maps this to its own PR/merge-request REST endpoint."""
        raise NotImplementedError

    def _post(self, url: str, *, json: dict, headers: dict[str, str]) -> dict:
        client = self._client or HttpClient(timeout=15.0)
        try:
            resp = client.post(url, json=json, headers=headers)
        except httpx.HTTPStatusError as exc:
            raise ForgeError(
                f"{self.provider} PR failed: {exc.response.status_code} {exc.response.text}"
            ) from exc
        except httpx.HTTPError as exc:
            raise ForgeError(f"{self.provider} PR request error: {exc}") from exc
        finally:
            if self._client is None:
                client.close()
        try:
            return resp.json()
        except ValueError as exc:  # non-JSON body
            raise ForgeError(f"{self.provider} PR returned non-JSON body") from exc


class ForgejoForge(_RestForge):
    """Forgejo / Gitea: ``POST /api/v1/repos/{owner}/{repo}/pulls``."""

    provider = "forgejo"

    def open_pull_request(self, *, head: str, base: str, title: str, body: str) -> PullRequest:
        url = f"{self._base}/api/v1/repos/{self._owner}/{self._repo}/pulls"
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"token {self._token}"
        data = self._post(
            url, json={"head": head, "base": base, "title": title, "body": body}, headers=headers
        )
        return PullRequest(
            number=int(data.get("number", 0)),
            url=str(data.get("html_url") or data.get("url") or ""),
            branch=head,
        )


class GitHubForge(_RestForge):
    """GitHub: ``POST /repos/{owner}/{repo}/pulls``."""

    provider = "github"

    def open_pull_request(self, *, head: str, base: str, title: str, body: str) -> PullRequest:
        url = f"{self._base}/repos/{self._owner}/{self._repo}/pulls"
        headers = {"Accept": "application/vnd.github+json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        data = self._post(
            url, json={"head": head, "base": base, "title": title, "body": body}, headers=headers
        )
        return PullRequest(
            number=int(data.get("number", 0)),
            url=str(data.get("html_url") or ""),
            branch=head,
        )


class GitLabForge(_RestForge):
    """GitLab: ``POST /api/v4/projects/{id}/merge_requests`` (a merge request)."""

    provider = "gitlab"

    def open_pull_request(self, *, head: str, base: str, title: str, body: str) -> PullRequest:
        project = quote(f"{self._owner}/{self._repo}", safe="")
        url = f"{self._base}/api/v4/projects/{project}/merge_requests"
        headers = {}
        if self._token:
            headers["PRIVATE-TOKEN"] = self._token
        data = self._post(
            url,
            json={
                "source_branch": head,
                "target_branch": base,
                "title": title,
                "description": body,
            },
            headers=headers,
        )
        return PullRequest(
            number=int(data.get("iid", 0)),
            url=str(data.get("web_url") or ""),
            branch=head,
        )


_FORGES: dict[str, type[_RestForge]] = {
    "forgejo": ForgejoForge,
    "gitea": ForgejoForge,
    "github": GitHubForge,
    "gitlab": GitLabForge,
}


def build_forge(gs: GitopsSettings) -> ForgeProvider | None:
    """Construct the deploy-repo forge client from settings, or None.

    None (the engine then refuses production+team direct writes rather than
    silently committing to main) when: gitops is off, push is off, there is no
    remote URL, or the URL cannot be parsed. Provider is ``gs.forge_provider`` or
    inferred from the host; the API base is ``gs.forge_api_base`` or derived.
    """
    if not gs.enabled or not gs.push or not gs.repo_url:
        return None
    try:
        coords = _split_repo_url(gs.repo_url)
    except ValueError as exc:
        logger.warning("gitops repo_url not usable for a review-PR forge", error=str(exc))
        return None

    provider = (gs.forge_provider or _infer_provider(coords.host)).lower()
    forge_cls = _FORGES.get(provider)
    if forge_cls is None:
        logger.warning("unknown gitops forge_provider; review PRs disabled", provider=provider)
        return None

    if gs.forge_api_base:
        api_base = gs.forge_api_base
    elif provider == "github":
        api_base = (
            "https://api.github.com"
            if coords.host.lower() == "github.com"
            else f"{coords.scheme}://{coords.host}/api/v3"
        )
    else:
        api_base = f"{coords.scheme}://{coords.host}"

    return forge_cls(api_base=api_base, owner=coords.owner, repo=coords.repo, token=gs.token)
