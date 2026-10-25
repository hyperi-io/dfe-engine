#  Project:      dfe-engine
#  File:         api/v1/usage.py
#  Purpose:      REST API for the usage numbers a deployment is licensed on
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Usage router: cores deployed, worker-node cores, bytes ingested and events per second.

    GET /usage/overview  -> every metric per UTC day over the last 30 days, and its
                            highest in the last 7
    GET /usage           -> one metric per UTC day over a range, for the deployment
                            or one org

Both read the usage rollup (:mod:`dfe_engine.usage`) and need ``usage:read`` at system
scope or at one of the caller's orgs. A caller whose grant reads every org sees the
whole deployment, and may name one org. Any other caller is held to the tenant ids of
the registered orgs it holds ``usage:read`` at: it reads their bytes and events per
second, and the two core counts,
which are measured for the deployment rather than per org, come back
``not_permitted``. A metric with no rows in the range is ``not_measured``, never 0.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import Annotated, Any, Literal

from dfe_schemas.usage import (
    ALL_ORGS,
    CORES_DEPLOYED,
    EVENTS_PER_SECOND,
    INGESTED_BYTES,
    METRICS,
    NODE_CORES,
    UsageMetric,
)
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from dfe_engine.api.deps import (
    ClickHouseClient,
    CurrentUser,
    is_action_allowed,
    reads_every_org,
    refuse_unknown_orgs,
    registered_orgs,
    require_own_org_action,
)
from dfe_engine.api.errors import ErrorResponse, backend_failure
from dfe_engine.auth.models import AuthContext, Scope
from dfe_engine.auth.rbac_scopes import scopes_dict
from dfe_engine.orgs.models import ORG_NAME_PATTERN
from dfe_engine.orgs.tenant_scope import org_tenant_ids, resolve_orgs
from dfe_engine.usage import (
    UsagePoint,
    UsageReader,
    UsageUnavailableError,
    usage_metric,
    usage_metric_names,
)

router = APIRouter(prefix="/usage", tags=["usage"])

_ACTION = scopes_dict["usage_read"]
_READ = Depends(require_own_org_action(_ACTION))

# The overview's series, and the window its highest is taken over, in UTC days to today.
OVERVIEW_DAYS = 30
HIGHEST_DAYS = 7
HIGHEST_LABEL = "Highest in the last 7 days"

# What an operator reads on a tile, keyed by the metric column dfe-schemas writes.
METRIC_LABELS = {
    CORES_DEPLOYED: "Cores deployed",
    NODE_CORES: "Worker-node cores",
    INGESTED_BYTES: "Data ingested",
    EVENTS_PER_SECOND: "Events per second",
}


class UsageStatus(StrEnum):
    """Whether a metric has a number for this caller."""

    MEASURED = "measured"
    NOT_MEASURED = "not_measured"
    NOT_PERMITTED = "not_permitted"


class UsageDay(BaseModel):
    """One UTC day of one metric."""

    day: date = Field(description="The UTC day")
    value: float = Field(
        description="The day's total for ingested_bytes, else the day's highest minute"
    )


class UsageMetricOverview(BaseModel):
    """One metric on the overview."""

    metric: str = Field(description="The metric: " + ", ".join(usage_metric_names()))
    label: str = Field(description="What a tile showing the metric is titled")
    unit: str = Field(description="What value counts: cores, bytes or events/s")
    status: UsageStatus = Field(
        description="not_measured when the range holds no row for the metric, and "
        "not_permitted when the caller is held to its orgs and the metric is not kept per org"
    )
    highest_7d: UsageDay | None = Field(
        description="The highest day in the last 7 UTC days, today included, and when"
    )
    latest: UsageDay | None = Field(description="The most recent day in the series")
    series: list[UsageDay] = Field(description="One entry per UTC day with a row, oldest first")


class UsageOverviewResponse(BaseModel):
    """Every usage metric over the last 30 UTC days."""

    orgs: list[str] = Field(
        description="The orgs the numbers cover, empty for the whole deployment"
    )
    first_day: date = Field(serialization_alias="from", description="The series' first UTC day")
    last_day: date = Field(serialization_alias="to", description="The series' last UTC day, today")
    highest_7d_label: str = Field(description="What the highest_7d number is captioned")
    metrics: list[UsageMetricOverview]


class UsageSeriesResponse(BaseModel):
    """One usage metric per UTC day over a range."""

    metric: str
    label: str
    unit: str
    status: UsageStatus
    granularity: Literal["day"]
    orgs: list[str] = Field(
        description="The orgs the numbers cover, empty for the whole deployment"
    )
    first_day: date = Field(serialization_alias="from", description="The range's first UTC day")
    last_day: date = Field(serialization_alias="to", description="The range's last UTC day")
    highest: UsageDay | None = Field(description="The highest day in the range, and when")
    latest: UsageDay | None = Field(description="The most recent day in the series")
    series: list[UsageDay] = Field(description="One entry per UTC day with a row, oldest first")


@dataclass(frozen=True, slots=True)
class UsageScope:
    """Whose usage one read covers.

    Attributes:
        held: The caller is held to its orgs, so a metric not kept per org is withheld.
        orgs: The org names covered, empty for the whole deployment.
        tenant_ids: The ``_org_id`` values read, ``[ALL_ORGS]`` for the whole deployment.
    """

    held: bool
    orgs: list[str]
    tenant_ids: list[str]


def usage_scope(request: Request, user: AuthContext, org: str | None) -> UsageScope:
    """The usage *user* may read, narrowed to *org* when it names one.

    A caller that reads every org with ``usage:read`` (:func:`reads_every_org`) reads
    the whole deployment, or a registered org it names. Any other caller reads the
    tenant ids of the registered orgs its groups name and allow ``usage:read`` at, or
    of the one it names.

    Args:
        request: The request, for the role definitions and the org registry.
        user: The caller's auth context.
        org: The one org to read, or None.

    Returns:
        The orgs and tenant ids the read covers.

    Raises:
        HTTPException: 422 ``unknown_org`` for an unregistered org a platform caller
            names, 403 ``org_not_granted`` for an org a held caller does not hold,
            registered or not, and 403 ``no_org`` for a held caller holding no
            registered org.
    """
    registered = registered_orgs(request)
    if reads_every_org(request, user, _ACTION):
        if org is None:
            return UsageScope(held=False, orgs=[], tenant_ids=[ALL_ORGS])
        refuse_unknown_orgs(request, [org])
        (named,) = [entry for entry in registered if entry.name == org]
        return UsageScope(held=False, orgs=[org], tenant_ids=org_tenant_ids(named))

    # An org the caller's groups name but grant no usage:read at is never read.
    granted = [
        entry
        for entry in resolve_orgs(user.org_ids, registered)
        if is_action_allowed(request, user, _ACTION, scope=Scope(type="org", id=entry.name))
    ]
    chosen = granted if org is None else [entry for entry in granted if entry.name == org]
    if org is not None and not chosen:
        raise HTTPException(
            status_code=403,
            detail={"code": "org_not_granted", "message": f"The caller holds no org '{org}'"},
        )
    names = [entry.name for entry in chosen]
    # '' is the whole deployment's row, which no held read may reach.
    tenant_ids = [
        tenant for entry in chosen for tenant in org_tenant_ids(entry) if tenant != ALL_ORGS
    ]
    if not tenant_ids:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "no_org",
                "message": "The caller's groups name no registered org, so it has no usage to read",
            },
        )
    return UsageScope(held=True, orgs=names, tenant_ids=sorted(set(tenant_ids)))


def _reader(request: Request, client: Any) -> UsageReader:
    return UsageReader(client, request.app.state.settings.clickhouse.effective_data_database)


def _unavailable(exc: UsageUnavailableError) -> HTTPException:
    """The 503 for a usage read ClickHouse refused, its text logged rather than sent."""
    return backend_failure(
        503,
        "usage_unavailable",
        "the usage tables could not be read, and the engine log has ClickHouse's reason",
        exc,
        event="usage query failed",
    )


def _read(
    reader: UsageReader,
    metric: UsageMetric,
    scope: UsageScope,
    *,
    first_day: date,
    last_day: date,
) -> tuple[UsageStatus, list[UsagePoint]]:
    """The metric's status and days for *scope*. A withheld metric reads nothing."""
    if scope.held and not metric.per_org:
        return UsageStatus.NOT_PERMITTED, []
    try:
        points = reader.daily(
            metric, scopes=scope.tenant_ids, first_day=first_day, last_day=last_day
        )
    except UsageUnavailableError as exc:
        raise _unavailable(exc) from exc
    return (UsageStatus.MEASURED if points else UsageStatus.NOT_MEASURED), points


def _day(point: UsagePoint | None) -> UsageDay | None:
    return None if point is None else UsageDay(day=point.day, value=point.value)


def _highest(points: list[UsagePoint]) -> UsagePoint | None:
    """The highest day, the earliest of equals, or None over no days."""
    if not points:
        return None
    return max(points, key=lambda point: point.value)


def _today() -> date:
    return datetime.now(UTC).date()


@router.get(
    "/overview",
    response_model=UsageOverviewResponse,
    responses={
        403: {
            "model": ErrorResponse,
            "description": "No usage:read, or the caller is held to its orgs and holds no "
            "registered one",
        },
        503: {"model": ErrorResponse, "description": "ClickHouse could not read the usage tables"},
    },
    dependencies=[_READ],
)
def get_usage_overview(
    request: Request, user: CurrentUser, client: ClickHouseClient
) -> UsageOverviewResponse:
    """Every usage metric per UTC day over the last 30 days, and its highest in the last 7.

    The whole deployment for a caller that reads every org, else the caller's own
    orgs combined, with the two core counts ``not_permitted``. Events per second over
    more than one tenant id is the busiest minute of their sum, read from the minute
    table, which keeps 7 days.
    """
    scope = usage_scope(request, user, None)
    last_day = _today()
    first_day = last_day - timedelta(days=OVERVIEW_DAYS - 1)
    highest_from = last_day - timedelta(days=HIGHEST_DAYS - 1)
    reader = _reader(request, client)
    metrics: list[UsageMetricOverview] = []
    for metric in METRICS:
        status, points = _read(reader, metric, scope, first_day=first_day, last_day=last_day)
        recent = [point for point in points if point.day >= highest_from]
        metrics.append(
            UsageMetricOverview(
                metric=metric.name,
                label=METRIC_LABELS[metric.name],
                unit=metric.unit,
                status=status,
                highest_7d=_day(_highest(recent)),
                latest=_day(points[-1] if points else None),
                series=[UsageDay(day=point.day, value=point.value) for point in points],
            )
        )
    return UsageOverviewResponse(
        orgs=scope.orgs,
        first_day=first_day,
        last_day=last_day,
        highest_7d_label=HIGHEST_LABEL,
        metrics=metrics,
    )


@router.get(
    "",
    response_model=UsageSeriesResponse,
    responses={
        403: {
            "model": ErrorResponse,
            "description": "No usage:read, or the caller is held to its orgs and does not "
            "hold the one named",
        },
        422: {
            "model": ErrorResponse,
            "description": "An unknown metric or org, a range ending before it starts, "
            "or an org named for a metric kept only for the whole deployment",
        },
        503: {"model": ErrorResponse, "description": "ClickHouse could not read the usage tables"},
    },
    dependencies=[_READ],
)
def get_usage(
    request: Request,
    user: CurrentUser,
    client: ClickHouseClient,
    metric: Annotated[
        str,
        Query(description="The metric to read", json_schema_extra={"enum": usage_metric_names()}),
    ],
    first_day: Annotated[
        date | None,
        Query(alias="from", description="The first UTC day, 29 days before `to` when absent"),
    ] = None,
    last_day: Annotated[
        date | None, Query(alias="to", description="The last UTC day, today when absent")
    ] = None,
    org: Annotated[
        str | None,
        Query(
            pattern=ORG_NAME_PATTERN,
            description="One org to read. Absent reads the whole deployment, or every org "
            "the caller holds",
        ),
    ] = None,
    granularity: Annotated[
        Literal["day"], Query(description="Per UTC day, the only granularity the rollup keeps")
    ] = "day",
) -> UsageSeriesResponse:
    """One usage metric per UTC day over a range, for the whole deployment or one org.

    Events per second over more than one tenant id is the busiest minute of their
    sum, read from the minute table, which keeps 7 days.
    """
    try:
        declared = usage_metric(metric)
    except KeyError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "unknown_metric",
                "message": f"No usage metric '{metric}', it is one of "
                f"{', '.join(usage_metric_names())}",
            },
        ) from exc
    last = last_day or _today()
    first = first_day or last - timedelta(days=OVERVIEW_DAYS - 1)
    if first > last:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "bad_range",
                "message": f"The range starts {first}, after it ends {last}",
            },
        )
    scope = usage_scope(request, user, org)
    if org is not None and not scope.held and not declared.per_org:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "not_per_org",
                "message": f"{metric} is measured for the whole deployment, not per org",
            },
        )
    status, points = _read(
        _reader(request, client), declared, scope, first_day=first, last_day=last
    )
    return UsageSeriesResponse(
        metric=declared.name,
        label=METRIC_LABELS[declared.name],
        unit=declared.unit,
        status=status,
        granularity=granularity,
        orgs=scope.orgs,
        first_day=first,
        last_day=last,
        highest=_day(_highest(points)),
        latest=_day(points[-1] if points else None),
        series=[UsageDay(day=point.day, value=point.value) for point in points],
    )
