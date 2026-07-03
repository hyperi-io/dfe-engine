#  Project:      dfe-engine
#  File:         gitcrud/auto_merge.py
#  Purpose:      Engine-wide auto-merge flag (solo/dev fast path) + its gate
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Auto-merge: force-direct commits for solo/dev deployments.

The stored flag lives in gitops (governance/settings/gitops.yaml, key
``auto_merge``) so the UI toggles it through the normal governed-ops CRUD path
and the toggle is itself an audited commit. The deployment gate decides whether
the flag may be enabled at all: dev posture (DFE_ENV, ~80% of the want) OR
gitops.mode=solo (solo-operator production, the rest). effective = stored AND
gate. Loud by design - see the spec
(docs/superpowers/specs/2026-07-03-gitcrud-auto-merge-design.md).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from scalo.logger import logger

from dfe_engine.settings import is_dev_posture

from .commit_policy import CommitContext, build_message, resolve_mode
from .engine import GitCrud, ResourceNotFoundError

if TYPE_CHECKING:
    from dfe_engine.gitops.repo import PublishResult

CLASS = "gov_settings"
NAME = "gitops"
KEY = "auto_merge"


@dataclass(frozen=True)
class AutoMergeState:
    """UI-facing auto-merge status: stored flag, gate verdict, net effect."""

    stored: bool  # the flag as committed in gitops
    allowed: bool  # the deployment gate permits enabling
    effective: bool  # stored AND allowed - what actually happens on writes
    reason: str  # human-readable gate explanation (UI copy)


def gate(environment: str, mode: str) -> tuple[bool, str]:
    """Deployment gate: dev posture OR gitops.mode=solo may enable auto-merge."""
    if is_dev_posture(environment):
        return True, f"dev posture (DFE_ENV={environment})"
    if mode == "solo":
        return True, "solo operator posture (DFE_GITOPS_MODE=solo)"
    return False, (
        f"production posture (DFE_ENV={environment}) with DFE_GITOPS_MODE=team; "
        "set DFE_GITOPS_MODE=solo (or a dev DFE_ENV) to permit auto-merge"
    )


def stored_flag(crud: GitCrud) -> bool:
    """The committed flag; absent resource/key means OFF."""
    try:
        return bool(crud.get(CLASS, NAME).get(KEY, False))
    except ResourceNotFoundError:
        return False


def resolve_state(crud: GitCrud, *, environment: str, mode: str) -> AutoMergeState:
    """stored AND gate -> effective; WARNs when the flag is stranded ON."""
    stored = stored_flag(crud)
    allowed, reason = gate(environment, mode)
    if stored and not allowed:
        logger.warning(
            "auto-merge is ON in gitops but the deployment gate refuses it; "
            "auto-merge is effectively OFF",
            reason=reason,
        )
    return AutoMergeState(
        stored=stored, allowed=allowed, effective=stored and allowed, reason=reason
    )


def set_stored(crud: GitCrud, enabled: bool, actor: str) -> PublishResult:
    """Commit the flag via the governed path (audited, direct commit).

    Direct on purpose: the gate only permits toggling in solo/dev deployments,
    which have no forge to PR against.
    """
    msg = build_message(
        CommitContext(
            ctype="rbac",
            scope=NAME,
            summary=f"auto-merge {'on' if enabled else 'off'}",
            actor=actor,
        )
    )
    return crud.set_key(CLASS, NAME, KEY, enabled, actor, message=msg)


def apply_auto_merge(
    state: AutoMergeState,
    *,
    environment: str,
    rbac_class: str,
    protected: bool = False,
    actor: str,
    resource: str,
) -> bool:
    """True when auto-merge converts a would-be PR write into a direct commit.

    Loud by design: every conversion WARNs with actor + resource so an
    auto-merged deployment is unmissable in the logs. Writes that were direct
    anyway return False (no badge, no noise).
    """
    if not state.effective:
        return False
    mode = resolve_mode(environment=environment, rbac_class=rbac_class, protected=protected)
    if mode != "pr":
        return False
    logger.warning(
        "AUTO-MERGE: direct commit to main in lieu of a PR",
        actor=actor,
        resource=resource,
    )
    return True


def startup_banner(state: AutoMergeState) -> None:
    """WARN banner at engine start when auto-merge is effectively ON."""
    if state.effective:
        logger.warning(
            "AUTO-MERGE IS ON: every governed-ops change commits straight to "
            "main and Argo applies it immediately - no PR, no review gate",
            reason=state.reason,
        )
