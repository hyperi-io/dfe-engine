#  Project:      dfe-engine
#  File:         gitcrud/retention.py
#  Purpose:      Admin-editable default retention, stored in the deploy repo
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The deployment default TTL an admin sets from the console.

The override lives in the deploy repo (``governance/settings/retention.yaml``, key
``default_ttl_days``), so it survives the loss of the engine and every change is an
audited commit. The effective default is the override when one is set, else
``settings.clickhouse.default_ttl_days``. Every surface that builds or reports a
table's retention resolves it here, which is what keeps a deploy, a plan, the boot
apply and the console on one value.

Reads go through gitcrud, which refreshes from the remote first, so a change made
on one engine replica is what every other replica builds with next.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from scalo.logger import logger

from dfe_engine.settings import DFESettings

from .commit_policy import CommitContext, build_message
from .engine import GitCrud, ResourceNotFoundError

if TYPE_CHECKING:
    from dfe_engine.gitops.repo import PublishResult

CLASS = "gov_settings"
NAME = "retention"
KEY = "default_ttl_days"

# 100 years; a longer retention is "keep forever", which is 0.
MAX_DEFAULT_TTL_DAYS = 36_500

Origin = Literal["override", "deployment"]


@dataclass(frozen=True, slots=True)
class RetentionState:
    """Where the effective default TTL comes from, and what it is.

    Attributes:
        stored: The override committed in the deploy repo; None when there is none.
        effective: Days a time-series table that declares no TTL gets; 0 means none.
        origin: ``override`` when the stored value wins, ``deployment`` otherwise.
        deployment_default: ``DFE_CLICKHOUSE_DEFAULT_TTL_DAYS`` as deployed.
    """

    stored: int | None
    effective: int
    origin: Origin
    deployment_default: int


def deployment_days(settings: Any) -> int:
    """``settings.clickhouse.default_ttl_days`` as an int, 0 when unset."""
    return int(getattr(settings.clickhouse, "default_ttl_days", 0) or 0)


def stored_days(crud: GitCrud) -> int | None:
    """The committed override, or None when there is none or it cannot be used.

    A hand-edited file that is malformed, not a mapping, or holds anything but a
    non-negative int reads as no override: the deployment default is always a safe
    answer, and a 500 on every read and every deploy is not.
    """
    try:
        value = crud.get(CLASS, NAME).get(KEY)
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


def resolve_state(crud: GitCrud | None, settings: Any) -> RetentionState:
    """The override when one is stored, else the deployment default.

    Without gitops there is nowhere to store an override, so the deployment default
    is the whole answer.
    """
    deployed = deployment_days(settings)
    stored = stored_days(crud) if crud is not None else None
    if stored is None:
        return RetentionState(
            stored=None, effective=deployed, origin="deployment", deployment_default=deployed
        )
    return RetentionState(
        stored=stored, effective=stored, origin="override", deployment_default=deployed
    )


def with_default_ttl_days(settings: DFESettings, days: int) -> DFESettings:
    """A copy of *settings* whose ``clickhouse.default_ttl_days`` is *days*."""
    clickhouse = settings.clickhouse.model_copy(update={"default_ttl_days": days})
    return settings.model_copy(update={"clickhouse": clickhouse})


def effective_settings(settings: DFESettings, crud: GitCrud | None) -> DFESettings:
    """*settings* with the stored table defaults applied.

    TTL, and the common-header and engine overrides from
    :func:`dfe_engine.gitcrud.table_defaults.resolve`, ride on a copy so the
    deployment's own values stay readable on the original. *settings* itself
    when nothing is stored. A deploy builds from this copy; retention reconcile
    reads the TTL off it and does not apply the header.
    """
    # Local import: table_defaults imports this module's constants.
    from dfe_engine.gitcrud.table_defaults import resolve as resolve_table_defaults

    updates: dict[str, Any] = {}
    state = resolve_state(crud, settings)
    if state.origin == "override":
        updates["default_ttl_days"] = state.effective
    table = resolve_table_defaults(crud, settings)
    if table.engine_origin == "override":
        updates["default_engine"] = table.engine
    if table.header_type_origin == "override":
        updates["default_header_type"] = table.header_type
    if table.header_version_origin == "override":
        updates["default_header_version"] = table.header_version
    if not updates:
        return settings
    clickhouse = settings.clickhouse.model_copy(update=updates)
    return settings.model_copy(update={"clickhouse": clickhouse})


def set_stored(crud: GitCrud, days: int | None, actor: str) -> PublishResult | None:
    """Commit the override, or remove it when *days* is None.

    A direct commit, like the auto-merge flag: this is engine-wide deployment config
    with no per-resource review flow. Clearing an override that is not there commits
    nothing and returns None.

    Raises:
        ValueError: *days* is negative or above :data:`MAX_DEFAULT_TTL_DAYS`.
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
    if not 0 <= days <= MAX_DEFAULT_TTL_DAYS:
        raise ValueError(f"default_ttl_days must be 0..{MAX_DEFAULT_TTL_DAYS}, not {days}")
    msg = build_message(
        CommitContext(ctype="cfg", scope=NAME, summary=f"default ttl {days} days", actor=actor)
    )
    return crud.set_key(CLASS, NAME, KEY, days, actor, message=msg)
