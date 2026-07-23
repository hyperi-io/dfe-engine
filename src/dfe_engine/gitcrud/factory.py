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

from scalo.logger import logger

from dfe_engine.gitops.repo import GitopsRepo
from dfe_engine.governance.seed import SEED_COMMIT_MESSAGE, pending_seed
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
    _seed_governance(repo)
    return GitCrud(repo)


def _seed_governance(repo: GitopsRepo) -> None:
    """Commit the shipped action library into a deploy repo that lacks it.

    Best-effort by design: a deployment whose git remote is briefly unreachable
    must still start and serve every other route. A failure here means the
    Governed Ops list is empty until the next restart, which is visible and
    recoverable - refusing to boot over it would not be.
    """
    try:
        root = repo.ensure()
        artifacts = pending_seed(repo_root=root)
        if not artifacts:
            return
        repo.publish(artifacts, SEED_COMMIT_MESSAGE)
        logger.info(
            f"Seeded {len(artifacts)} governance file(s) into the deploy repo: "
            f"{', '.join(sorted(artifacts))}"
        )
    except Exception as exc:
        logger.warning(f"Governance seed skipped: {exc}")
