#  Project:      dfe-engine
#  File:         gitcrud/routing.py
#  Purpose:      Route a governed write to main (direct) or to a review PR
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Direct-to-main vs review-PR, decided BEFORE the write and enforced in code.

The auto-merge deployment gate (gitcrud/auto_merge.py) says a production DFE_ENV
with DFE_GITOPS_MODE=team may NOT auto-merge, and the design says that posture has
NO escape hatch: a governed write must not land straight on the deploy repo's main
(CLAUDE.md gitops section; docs/control-plane/gitops-commit-standard.md section 4). Historically
that refusal was only a response badge -- the commit+push happened first
regardless. ``route_write`` closes that: when the gate refuses, the write is
committed to a short-lived branch and a review PR is opened via the forge seam;
if no forge is configured it HARD-FAILS (never main). Dev / solo / effective
auto-merge keep committing straight to main -- the sanctioned fast path, unchanged.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from scalo.logger import logger

from .auto_merge import apply_auto_merge, resolve_state
from .commit_policy import resolve_mode

if TYPE_CHECKING:
    from collections.abc import Callable

    from .engine import GitCrud
    from .forge import ForgeProvider


class ReviewRequiredError(Exception):
    """A production+team write may not hit main and no forge can open a PR.

    Surfaced by the API as 409: the operator must configure a forge (or switch to
    a solo/dev posture) -- there is deliberately no direct-to-main fallback.
    """


class _Published(Protocol):
    """The subset of PublishResult / InvokeResult route_write consumes."""

    changed: bool
    commit_sha: str | None


@dataclass
class WriteOutcome:
    """What route_write did: a direct commit, an opened PR, or a no-op."""

    changed: bool
    commit_sha: str | None = None
    auto_merged: bool = False
    review_required: bool = False
    pr_url: str | None = None
    branch: str | None = None


@dataclass
class ReviewRouting:
    """Where a MULTI-write run's changes landed, collected across its writes.

    A run that writes several resources gets one outcome per write, so a caller
    reporting the run as applied has to fold them together -- the run is
    review-required as soon as ANY of its writes was routed off main.
    """

    review_required: bool = False
    branches: list[str] = field(default_factory=list)
    pr_urls: list[str] = field(default_factory=list)

    def record(self, outcome: WriteOutcome | None) -> None:
        """Fold one write's outcome in; a direct or off-gitops write adds nothing."""
        if outcome is None or not outcome.review_required:
            return
        self.review_required = True
        if outcome.branch and outcome.branch not in self.branches:
            self.branches.append(outcome.branch)
        if outcome.pr_url and outcome.pr_url not in self.pr_urls:
            self.pr_urls.append(outcome.pr_url)

    @property
    def first_pr_url(self) -> str | None:
        """The PR to send an operator to first; the rest ride on the response body."""
        return self.pr_urls[0] if self.pr_urls else None


def pr_branch_name(rbac_class: str, resource: str, request_id: str) -> str:
    """``dfe/<class>/<slug>/<request-id>`` -- a safe, unique review-branch name."""
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", resource).strip("-") or "resource"
    rid = request_id or uuid.uuid4().hex[:8]
    return f"dfe/{rbac_class}/{slug}/{rid}"


def route_write(
    *,
    gc: GitCrud,
    forge: ForgeProvider | None,
    environment: str,
    mode: str,
    rbac_class: str,
    resource: str,
    actor: str,
    title: str,
    body: str,
    write: Callable[[str], _Published],
    protected: bool = False,
    request_id: str = "",
) -> WriteOutcome:
    """Resolve direct-vs-PR, then perform the write on the chosen target.

    ``write(branch)`` does the actual gitcrud write: ``branch=""`` commits to the
    tracked branch (main); a non-empty branch commits to that review branch. It
    must return an object exposing ``changed`` + ``commit_sha`` (PublishResult or
    InvokeResult). Enforcement is scoped to the auto-merge gate refusal (team +
    production); every other posture keeps its current direct-to-main behaviour.
    """
    state = resolve_state(gc, environment=environment, mode=mode)
    resolved = resolve_mode(
        environment=environment,
        rbac_class=rbac_class,
        protected=protected,
        auto_merge=state.effective,
    )
    # state.allowed is the deployment gate: True in dev / solo (direct-commit is
    # sanctioned there), False only in production+team -- the posture this guard
    # exists for. Route to a PR ONLY when the gate refuses AND the change would
    # otherwise have gone direct to main.
    if resolved != "pr" or state.allowed:
        res = write("")
        auto_merged = False
        if res.changed:
            auto_merged = apply_auto_merge(
                state,
                environment=environment,
                rbac_class=rbac_class,
                protected=protected,
                actor=actor,
                resource=resource,
                commit_sha=res.commit_sha,
            )
        return WriteOutcome(changed=res.changed, commit_sha=res.commit_sha, auto_merged=auto_merged)

    # Production + team: refuse direct-to-main. Route to a review branch + PR.
    if forge is None:
        raise ReviewRequiredError(
            "direct-to-main refused in production with DFE_GITOPS_MODE=team; a "
            "review PR is required but no git forge is configured (set "
            "DFE_GITOPS_FORGE_* / DFE_GITOPS_REPO_URL, or use a solo/dev posture)"
        )
    branch = pr_branch_name(rbac_class, resource, request_id)
    res = write(branch)
    if not res.changed:
        # A no-op write (same value) -- nothing pushed, no PR to open.
        return WriteOutcome(changed=False)
    pr = forge.open_pull_request(head=branch, base=gc.repo.branch, title=title, body=body)
    logger.warning(
        "REVIEW REQUIRED: production+team governed write routed to a PR (not main)",
        actor=actor,
        resource=resource,
        branch=branch,
        pr=pr.url,
    )
    return WriteOutcome(
        changed=True,
        commit_sha=res.commit_sha,
        review_required=True,
        pr_url=pr.url,
        branch=branch,
    )
