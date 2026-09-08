#  Project:      dfe-engine
#  File:         gitcrud/retention.py
#  Purpose:      Console-editable default retention, stored in the deploy repo
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The deployment default TTL an operator sets from the console.

The override lives in gitops (governance/settings/retention.yaml, key
``default_ttl_days``) so it survives the loss of the engine and every write is
an audited commit. The effective default is the stored override when one is
set, else ``settings.clickhouse.default_ttl_days``; every reader resolves it
through :func:`effective_default_ttl_days` so no two surfaces can disagree.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from scalo.logger import logger

from .commit_policy import CommitContext, build_message
from .engine import GitCrud, ResourceNotFoundError

if TYPE_CHECKING:
    from dfe_engine.gitops.repo import PublishResult

CLASS = "gov_settings"
NAME = "retention"
KEY = "default_ttl_days"

Origin = Literal["override", "deployment"]


@dataclass(frozen=True)
class RetentionState:
    """Where the effective default TTL comes from, and what it is."""

    stored: int | None  # the override as committed in gitops, None when unset
    effective: int  # what every time-series table without its own TTL gets
    origin: Origin  # "override" when stored wins, else "deployment"


def stored_days(crud: GitCrud) -> int | None:
    """The committed override; absent resource/key or an unreadable file means none.

    A hand-edited file that is malformed, a non-mapping document, or a value that
    is not a non-negative int is treated as no override rather than raising: the
    deployment default is always a safe answer, a 500 on every read is not.
    """
    try:
        doc = crud.get(CLASS, NAME)
        value = doc.get(KEY)
    except ResourceNotFoundError:
        return None
    except Exception as exc:
        logger.warning(
            "retention override file is unreadable; using the deployment default",
            error=str(exc),
        )
        return None
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        logger.warning(
            "retention override is not a non-negative int; using the deployment default",
            value=repr(value),
        )
        return None
    return value


def deployment_days(settings: Any) -> int:
    """``settings.clickhouse.default_ttl_days`` as an int, 0 when unset."""
    return int(getattr(settings.clickhouse, "default_ttl_days", 0) or 0)


def resolve_state(crud: GitCrud, settings: Any) -> RetentionState:
    """Stored override when set, else the deployment default."""
    stored = stored_days(crud)
    if stored is None:
        return RetentionState(stored=None, effective=deployment_days(settings), origin="deployment")
    return RetentionState(stored=stored, effective=stored, origin="override")


def effective_default_ttl_days(settings: Any, crud: GitCrud | None) -> int:
    """The one resolver every reader uses; without gitops it is the env value."""
    if crud is None:
        return deployment_days(settings)
    return resolve_state(crud, settings).effective


def set_stored(crud: GitCrud, days: int | None, actor: str) -> PublishResult | None:
    """Commit the override, or remove it when *days* is None.

    Direct on purpose, like the auto-merge flag: the resource is engine-wide
    deployment config with no per-resource review flow. Clearing an override
    that is not there is a no-op and returns None.
    """
    if days is None:
        if stored_days(crud) is None:
            return None
        msg = build_message(
            CommitContext(
                ctype="cfg", scope=NAME, summary="clear the default ttl override", actor=actor
            )
        )
        return crud.delete_key(CLASS, NAME, KEY, actor, message=msg)
    if days < 0:
        raise ValueError("default_ttl_days must be >= 0")
    msg = build_message(
        CommitContext(ctype="cfg", scope=NAME, summary=f"default ttl {days} days", actor=actor)
    )
    return crud.set_key(CLASS, NAME, KEY, days, actor, message=msg)
