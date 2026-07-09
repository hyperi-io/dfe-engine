#  Project:      dfe-engine
#  File:         gitcrud/factory.py
#  Purpose:      Build a GitCrud from settings (None when gitops is disabled)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Construct the Governed Ops engine from settings.

Returns None when gitops is disabled or unconfigured, so app startup is unaffected
and the Governed Ops routers degrade to 503 (not_configured) like other optional
features.
"""

from __future__ import annotations

from dfe_engine.gitops.repo import GitopsRepo
from dfe_engine.settings import GitopsSettings

from .engine import GitCrud


def build_gitcrud(gs: GitopsSettings) -> GitCrud | None:
    """Build a GitCrud over the deploy repo, or None if gitops is off/unconfigured."""
    if not gs.enabled or not gs.local_path:
        return None
    repo = GitopsRepo(
        local_path=gs.local_path,
        repo_url=gs.repo_url,
        branch=gs.branch,
        push=gs.push,
        username=gs.username,
        token=gs.token,
        author_name=gs.author_name,
        author_email=gs.author_email,
    )
    return GitCrud(repo)
