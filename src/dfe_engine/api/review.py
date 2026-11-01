#  Project:      dfe-engine
#  File:         api/review.py
#  Purpose:      Signal a routed-to-review governed write on a fixed-body response
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Response headers that say a governed write went to review, not to main.

A write endpoint whose body is a WriteResult carries ``review_required`` and
``pr_url`` in the body. The endpoints whose body is the resource itself (or a 204
with no body at all) cannot, so they say the same thing in headers -- otherwise a
production+team write reads as applied while it sits on a review branch the
runner never syncs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from fastapi import Response

    from dfe_engine.gitcrud.routing import ReviewRouting, WriteOutcome


def set_review_headers(response: Response, *, review_required: bool, pr_url: str | None) -> None:
    """Set the review headers from already-unpacked fields.

    The header pair is the same whether it came from one write's outcome or from a
    multi-write run folded into a ``ReviewRouting``, so both go through here.
    """
    if not review_required:
        return
    response.headers["X-DFE-Review-Required"] = "true"
    if pr_url:
        response.headers["X-DFE-PR-Url"] = pr_url


def apply_review_headers(response: Response, outcome: WriteOutcome | None) -> None:
    """Mark a fixed-body 200/201/204 response whose write was routed to review."""
    if outcome is None:
        return
    set_review_headers(response, review_required=outcome.review_required, pr_url=outcome.pr_url)


def review_audit_detail(outcome: WriteOutcome | None) -> dict[str, Any] | None:
    """Audit detail for a governed write, naming the review branch when there is one.

    An audit line reading "created" against a change parked on a review branch is
    the record an operator would act on, so the branch and PR ride along with it.
    """
    if outcome is None:
        return None
    detail: dict[str, Any] = {"commit": outcome.commit_sha}
    if outcome.review_required:
        detail["review_required"] = True
        detail["branch"] = outcome.branch
        detail["pr_url"] = outcome.pr_url
    return detail


def routing_audit_detail(routing: ReviewRouting) -> dict[str, Any] | None:
    """Audit detail for writes folded into *routing*, or None when every one reached main."""
    if not routing.review_required:
        return None
    return {"review_required": True, "branches": routing.branches, "pr_urls": routing.pr_urls}


class ReviewFields(BaseModel):
    """Whether the write went to a review branch rather than main, as a governed write reports it."""

    review_required: bool = Field(
        default=False,
        description=(
            "True when production+team sent the write to a review branch: main is "
            "unchanged and the apps follow once the branch is merged"
        ),
    )
    pr_url: str | None = Field(
        default=None, description="The review PR, where a forge is configured to open one"
    )
    review_branch: str | None = Field(
        default=None, description="The branch carrying the write, while it awaits review"
    )


def review_fields(routing: ReviewRouting | None) -> dict[str, Any]:
    """The :class:`ReviewFields` values for writes folded into *routing*, empty when all reached main."""
    if routing is None or not routing.review_required:
        return {}
    return {
        "review_required": True,
        "pr_url": routing.first_pr_url,
        "review_branch": routing.branches[0] if routing.branches else None,
    }


# What a write held for review answers in ``message`` in place of what it would have done.
AWAITING_REVIEW = "awaiting review"
