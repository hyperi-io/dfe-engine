#  Project:      dfe-engine
#  File:         api/v1/kafka_topics.py
#  Purpose:      Governed topic contract for the sources this deployment carries
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The Kafka topic contract a defined DFE source implies (dfe-engine#97).

GET  /api/v1/kafka/topics         -> per-source existence, shape, config, drift
POST /api/v1/kafka/topics/ensure  -> create whatever is missing (idempotent)
POST /api/v1/kafka/topics/update  -> converge configs and widen partitions
POST /api/v1/kafka/topics/remove  -> delete a source's topics (destructive)

The topic set is DERIVED from the sources, never typed by a caller: a source
always needs ``<source>_land``, and one with a transform also needs
``<source>_load``. That is what makes this governed rather than a broker admin
shell - the request names a source, and the engine decides which topics follow.

RBAC rides the source scopes for the same reason: the right to deploy a source
is the right to make its topics exist, and the right to delete one is the right
to take them away. Every route reports the broker being unreachable rather than
failing, because a deployment whose bus is still coming up is not a bad request.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from dfe_engine.api.deps import CurrentUser, Settings, SourceReg, require_action
from dfe_engine.auth.audit import audit_resource_change
from dfe_engine.auth.rbac_scopes import scopes_dict
from dfe_engine.kafka.topics import (
    TopicSpec,
    ensure_topics,
    remove_topics,
    source_topic_names,
    specs_for_sources,
    topic_status,
    topics_managed,
    update_topics,
)
from dfe_engine.settings import DFESettings
from dfe_engine.source.registry import SourceNotFoundError

router = APIRouter(prefix="/kafka/topics", tags=["Kafka Topics"])


class TopicFailure(BaseModel):
    """One topic the broker would not do the thing to, and what it said."""

    name: str
    error: str


class TopicStateResponse(BaseModel):
    """What the broker holds for one topic, against what its source asks for."""

    name: str
    source: str
    exists: bool
    desired_partitions: int
    desired_replication_factor: int
    partitions: int | None = None
    replication_factor: int | None = None
    config: dict[str, str] = Field(default_factory=dict)
    drift: list[str] = Field(default_factory=list)


class TopicStatusResponse(BaseModel):
    """Status for every topic the sources in scope imply."""

    reachable: bool = Field(description="False when the broker could not be read at all")
    error: str | None = Field(
        default=None, description="Why the broker could not be read; null when it could"
    )
    topics: list[TopicStateResponse] = Field(default_factory=list)


class TopicEnsureResponse(BaseModel):
    """Outcome of an ensure pass: every topic in scope lands in exactly one list."""

    created: list[str] = Field(default_factory=list)
    existing: list[str] = Field(default_factory=list)
    failed: list[TopicFailure] = Field(default_factory=list)
    dry_run: bool = False


class TopicUpdateResponse(BaseModel):
    """Outcome of a converge pass.

    ``refused`` is what DFE declined to do (a partition decrease, a replication
    factor change); ``failed`` is what the broker rejected. They are different
    problems with different fixes, so they are different lists.
    """

    altered: list[str] = Field(default_factory=list)
    widened: list[str] = Field(default_factory=list)
    unchanged: list[str] = Field(default_factory=list)
    absent: list[str] = Field(default_factory=list)
    refused: list[TopicFailure] = Field(default_factory=list)
    failed: list[TopicFailure] = Field(default_factory=list)
    dry_run: bool = False


class TopicRemoveResponse(BaseModel):
    """Outcome of a remove pass."""

    removed: list[str] = Field(default_factory=list)
    absent: list[str] = Field(default_factory=list)
    failed: list[TopicFailure] = Field(default_factory=list)
    dry_run: bool = False


def _failures(pairs: list[tuple[str, str]]) -> list[TopicFailure]:
    return [TopicFailure(name=name, error=error) for name, error in pairs]


def _require_managed(settings: DFESettings) -> None:
    """Refuse every route when this deployment does not manage its topics.

    A brokerless profile has no bus to talk to, and answering with an empty list
    would read as "no topics needed" rather than "not this deployment's job".
    """
    if not topics_managed(settings):
        raise HTTPException(
            status_code=503,
            detail={
                "code": "not_configured",
                "message": (
                    "this deployment does not manage Kafka topics: it runs no bus, or "
                    "kafka.ensure_topics is off"
                ),
            },
        )


def _specs_in_scope(
    registry: Any, settings: DFESettings, source: str | None
) -> tuple[list[TopicSpec], dict[str, str]]:
    """The specs and their owners for one source, or for every source."""
    if source is None:
        return specs_for_sources(registry.get_all_sources(), settings)
    try:
        one = registry.get_source(source)
    except SourceNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"unknown source '{source}'"},
        ) from exc
    return specs_for_sources([one], settings)


_SOURCE_QUERY = Query(None, description="Limit to one source's topics; omitted means every source")
_DRY_RUN_QUERY = Query(False, description="Report what would change without changing it")


@router.get(
    "",
    response_model=TopicStatusResponse,
    dependencies=[Depends(require_action(scopes_dict["source_read"]))],
)
async def list_topic_status(
    user: CurrentUser,
    registry: SourceReg,
    settings: Settings,
    source: str | None = _SOURCE_QUERY,
) -> TopicStatusResponse:
    """Report each source topic's existence, shape, config and drift.

    Read-only: it creates nothing, so a status call against a broker mid-restart
    reports that it could not be read rather than reporting every topic missing.
    """
    _require_managed(settings)
    specs, owners = _specs_in_scope(registry, settings, source)
    outcome = await asyncio.to_thread(topic_status, specs, sources=owners, settings=settings)
    return TopicStatusResponse(
        reachable=outcome.reachable,
        error=outcome.error,
        topics=[TopicStateResponse(**vars(state)) for state in outcome.topics],
    )


@router.post(
    "/ensure",
    response_model=TopicEnsureResponse,
    dependencies=[Depends(require_action(scopes_dict["source_deploy"]))],
)
async def ensure_source_topics(
    user: CurrentUser,
    registry: SourceReg,
    settings: Settings,
    source: str | None = _SOURCE_QUERY,
    dry_run: bool = _DRY_RUN_QUERY,
) -> TopicEnsureResponse:
    """Create every topic the sources in scope need and do not have.

    Idempotent, and it never touches a topic that already exists: widening an
    existing topic is ``update``, deliberately, so a re-ensure cannot reshape a
    live topic as a side effect.
    """
    _require_managed(settings)
    specs, _ = _specs_in_scope(registry, settings, source)
    outcome = await asyncio.to_thread(ensure_topics, specs, settings=settings, dry_run=dry_run)
    if not dry_run:
        audit_resource_change(
            user.user_id, "kafka_topics", source or "*", "ensured", {"created": outcome.created}
        )
    return TopicEnsureResponse(
        created=outcome.created,
        existing=outcome.existing,
        failed=_failures(outcome.failed),
        dry_run=dry_run,
    )


@router.post(
    "/update",
    response_model=TopicUpdateResponse,
    dependencies=[Depends(require_action(scopes_dict["source_deploy"]))],
)
async def update_source_topics(
    user: CurrentUser,
    registry: SourceReg,
    settings: Settings,
    source: str | None = _SOURCE_QUERY,
    dry_run: bool = _DRY_RUN_QUERY,
) -> TopicUpdateResponse:
    """Converge existing topics onto their spec: alter configs, widen partitions.

    A topic that does not exist is reported as absent rather than created, so a
    source that never deployed shows up here instead of being quietly fixed.
    """
    _require_managed(settings)
    specs, _ = _specs_in_scope(registry, settings, source)
    outcome = await asyncio.to_thread(update_topics, specs, settings=settings, dry_run=dry_run)
    if not dry_run:
        audit_resource_change(
            user.user_id,
            "kafka_topics",
            source or "*",
            "updated",
            {"altered": outcome.altered, "widened": outcome.widened},
        )
    return TopicUpdateResponse(
        altered=outcome.altered,
        widened=outcome.widened,
        unchanged=outcome.unchanged,
        absent=outcome.absent,
        refused=_failures(outcome.refused),
        failed=_failures(outcome.failed),
        dry_run=dry_run,
    )


@router.post(
    "/remove",
    response_model=TopicRemoveResponse,
    dependencies=[Depends(require_action(scopes_dict["source_delete"]))],
)
async def remove_source_topics(
    user: CurrentUser,
    registry: SourceReg,
    settings: Settings,
    source: str = Query(description="The source whose topics go; never every source at once"),
    confirm: bool = Query(
        False, description="Must be true: the topic's records go with it and do not come back"
    ),
    dry_run: bool = _DRY_RUN_QUERY,
) -> TopicRemoveResponse:
    """Delete one source's topics. DESTRUCTIVE - the records on them are gone.

    Guarded twice over: a source name is required (there is no remove-everything
    call), and ``confirm`` must be set. A dry run needs neither the confirmation
    nor a broker write, so it is the safe way to see what would go.
    """
    _require_managed(settings)
    if not confirm and not dry_run:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "confirmation_required",
                "message": (
                    f"deleting '{source}' topics destroys the records on them; pass "
                    "confirm=true, or dry_run=true to see what would go"
                ),
            },
        )
    try:
        one = registry.get_source(source)
    except SourceNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"unknown source '{source}'"},
        ) from exc

    outcome = await asyncio.to_thread(
        remove_topics, source_topic_names(one), settings=settings, dry_run=dry_run
    )
    if not dry_run:
        audit_resource_change(
            user.user_id, "kafka_topics", source, "removed", {"removed": outcome.removed}
        )
    return TopicRemoveResponse(
        removed=outcome.removed,
        absent=outcome.absent,
        failed=_failures(outcome.failed),
        dry_run=dry_run,
    )
