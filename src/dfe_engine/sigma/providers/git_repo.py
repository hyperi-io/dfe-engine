#  Project:      dfe-engine
#  File:         sigma/providers/git_repo.py
#  Purpose:      Sigma provider: clone/pull a git repo of *.yml rules (dulwich)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Git-repo sigma provider - the default OOTB feed (github.com/SigmaHQ/sigma).

Clones (or fast-forward-refreshes) a git repo via dulwich - the SAME pure-Python
git the gitcrud/gitops layers use, so there is no git binary and no new dependency
- then scans a rules subdir for *.yml and normalises each via pySigma. Serves the
SigmaHQ default AND any public/private git repo (token via the scalo.secrets seam).

Incremental: the design allows a commit-diff-since-last-sync optimisation where
practical; this adapter takes the simpler robust path - a full scan plus a
client-side `modified >= since` filter. The store's upsert then skips everything
that did not actually change, so a re-sync is cheap regardless. Commit-diff is a
noted future optimisation, not a correctness requirement.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

from scalo.logger import logger

from dfe_engine.gitops.dulwich_auth import authed_https_url, scrub_remote_credentials

from .base import ProviderConfig, SigmaProvider, SigmaRuleDoc, modified_since, parse_sigma_yaml


class GitRepoProvider(SigmaProvider):
    """Fetch sigma rules from a git repository (clone once, refresh thereafter)."""

    def __init__(self, config: ProviderConfig, *, secrets=None, work_dir: str | Path | None = None):
        super().__init__(config, secrets=secrets)
        # A persistent cache root so a re-sync refreshes the same clone instead of
        # re-cloning every time. Falls back to a temp dir when none is supplied.
        if work_dir is None:
            import tempfile

            work_dir = Path(tempfile.gettempdir()) / "dfe-sigma-cache"
        self._work_dir = Path(work_dir)

    @property
    def _clone_path(self) -> Path:
        return self._work_dir / self.config.name

    def _authed_url(self, url: str) -> str:
        """Embed HTTPS token creds in the URL (git_token auth); pass others through.

        Shared with GitopsRepo via
        :func:`~dfe_engine.gitops.dulwich_auth.authed_https_url` - a local-path or
        SSH url, or a no-auth public repo, is returned unchanged.
        """
        return authed_https_url(url, self.config.auth.username, self._secret())

    def _clone_or_refresh(self) -> None:
        """Clone the repo if absent, else fetch + hard-reset to the remote branch tip.

        The clone is a disposable read-only cache (DFE never commits to it), so a
        hard reset to the fetched branch sha is the simplest always-latest refresh -
        no merge, no conflict handling. Same fetch+reset dulwich pattern GitopsRepo
        uses to reconcile a diverged push.
        """
        from dulwich import porcelain

        url = str(self.config.options.get("url", "")).strip()
        if not url:
            raise ValueError(f"git_repo provider {self.name!r} has no 'url' option")
        branch = str(self.config.options.get("branch", "main")).strip() or "main"
        authed = self._authed_url(url)
        local = self._clone_path

        if (local / ".git").exists():
            result = porcelain.fetch(str(local), authed)
            sha = (result.refs or {}).get(f"refs/heads/{branch}".encode())
            if sha:
                porcelain.reset(str(local), "hard", sha)
            else:
                logger.warning(
                    "sigma git provider: branch not found on remote; using cached clone",
                    provider=self.name,
                    branch=branch,
                )
            return
        local.parent.mkdir(parents=True, exist_ok=True)
        logger.info("sigma git provider: cloning", provider=self.name, url=url, branch=branch)
        porcelain.clone(authed, str(local), branch=branch.encode())
        # porcelain.clone persists the cloned URL (incl the username:token@ auth
        # _authed_url embedded) into the cache clone's .git/config on disk under
        # config_dir/.sigma-cache - readable by any co-located sidecar/volume
        # snapshot. Scrub it back to the bare URL (fetch re-supplies auth each
        # call), mirroring GitopsRepo._scrub_remote_credentials.
        self._scrub_credentials(url)

    def _scrub_credentials(self, bare_url: str) -> None:
        """Rewrite remote.origin.url back to the credential-free URL after a clone.

        Shared scrub (see GitopsRepo); best-effort here - a cache clone that keeps
        its credential is a hardening miss, not a sync failure.
        """
        if not self._secret():
            return
        try:
            scrub_remote_credentials(self._clone_path, bare_url)
        except Exception:  # scrubbing is best-effort hardening, never fatal
            logger.warning(
                "sigma git provider: could not scrub clone credentials", provider=self.name
            )

    def _scan(self, since: datetime | None) -> list[SigmaRuleDoc]:
        subdir = str(self.config.options.get("subdir", "")).strip().strip("/")
        root = self._clone_path / subdir if subdir else self._clone_path
        docs: list[SigmaRuleDoc] = []
        warnings: list[str] = []
        if not root.is_dir():
            logger.warning(
                "sigma git provider: rules subdir missing", provider=self.name, subdir=subdir
            )
            self.last_warnings = warnings
            return docs
        for path in sorted(root.rglob("*.yml")):
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except OSError as exc:
                warnings.append(f"read failed {path}: {exc}")
                continue
            source_ref = str(path.relative_to(self._clone_path))
            parsed, warns = parse_sigma_yaml(text, self.origin, source_ref)
            docs.extend(parsed)
            warnings.extend(f"{source_ref}: {w}" for w in warns)
        if since is not None:
            docs = [d for d in docs if modified_since(d, since)]
        self.last_warnings = warnings
        return docs

    def _scrub_error(self, message: str, bare_url: str) -> str:
        """Strip the embedded git credential from an error before it surfaces.

        The fetch/clone URL carries ``username:token@`` (``_authed_url``); a dulwich
        failure can echo it into the exception message, which flows to the sync task
        result + logs. Redact the secret and rewrite the authed URL to the bare one.
        """
        secret = self._secret()
        if secret:
            message = message.replace(secret, "[REDACTED]")
        return message.replace(self._authed_url(bare_url), bare_url)

    def _fetch_sync(self, since: datetime | None) -> list[SigmaRuleDoc]:
        url = str(self.config.options.get("url", "")).strip()
        try:
            self._clone_or_refresh()
        except Exception as exc:
            # Never let a dulwich error leak the authed remote URL to the caller/logs.
            raise RuntimeError(self._scrub_error(str(exc), url)) from None
        return self._scan(since)

    async def fetch(self, since: datetime | None = None) -> list[SigmaRuleDoc]:
        """Clone/refresh + scan off the event loop (dulwich + file IO are blocking)."""
        return await asyncio.to_thread(self._fetch_sync, since)
