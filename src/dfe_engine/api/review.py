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

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import Response

    from dfe_engine.gitcrud.routing import WriteOutcome


def apply_review_headers(response: Response, outcome: WriteOutcome | None) -> None:
    """Mark a fixed-body 200/201/204 response whose write was routed to review."""
    if outcome is None or not outcome.review_required:
        return
    response.headers["X-DFE-Review-Required"] = "true"
    if outcome.pr_url:
        response.headers["X-DFE-PR-Url"] = outcome.pr_url


def review_audit_detail(outcome: WriteOutcome | None) -> dict | None:
    """Audit detail for a governed write, naming the review branch when there is one.

    An audit line reading "created" against a change parked on a review branch is
    the record an operator would act on, so the branch and PR ride along with it.
    """
    if outcome is None:
        return None
    detail: dict = {"commit": outcome.commit_sha}
    if outcome.review_required:
        detail["review_required"] = True
        detail["branch"] = outcome.branch
        detail["pr_url"] = outcome.pr_url
    return detail
