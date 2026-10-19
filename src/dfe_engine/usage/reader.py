#  Project:      dfe-engine
#  File:         usage/reader.py
#  Purpose:      Read one usage metric per UTC day from the usage rollup
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""One usage metric per UTC day, for the whole deployment or a set of orgs.

dfe-schemas declares the rollup (``tables/usage/``) and the metrics it carries
(:mod:`dfe_schemas.usage`). Its refreshable views write one row per metric, scope
and UTC minute to ``usage_minute``, and per UTC day to ``usage_daily``. A scope is
one ``_org_id``, or :data:`~dfe_schemas.usage.ALL_ORGS` for the whole deployment.

A day over several scopes is the sum of their days for a summed metric. For a
metric whose day is its highest minute, it is the highest minute of their sum,
which only ``usage_minute`` holds, and that table keeps seven days.

The queries live in ``queries.yaml`` beside this module, as
:class:`~dfe_engine.appmgmt.operations.OperationalReader` keeps its own, with every
value bound. A failed read raises rather than reading as no usage.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from dfe_schemas.usage import METRICS, UsageMetric

from dfe_engine.yaml_utils import yaml_load

_QUERIES = Path(__file__).parent / "queries.yaml"

# The database lands in an identifier position, so anything outside this is refused.
_IDENT = re.compile(r"[A-Za-z0-9_-]+")

# The daily aggregate dfe-schemas gives a metric whose day is its highest minute.
_DAILY_MAX = "max"

_METRICS = {metric.name: metric for metric in METRICS}


class UsageUnavailableError(RuntimeError):
    """Raised when the usage tables cannot be read."""


@dataclass(frozen=True, slots=True)
class UsagePoint:
    """One UTC day of one metric.

    Attributes:
        day: The UTC day.
        value: The day's total for a summed metric, else its highest minute.
    """

    day: date
    value: float


def usage_metric(name: str) -> UsageMetric:
    """The usage metric dfe-schemas declares under *name*.

    Args:
        name: The ``metric`` column value, such as ``ingested_bytes``.

    Returns:
        The metric's declaration.

    Raises:
        KeyError: dfe-schemas declares no metric of that name.
    """
    return _METRICS[name]


def usage_metric_names() -> list[str]:
    """Every usage metric's name, in the order dfe-schemas declares them."""
    return [metric.name for metric in METRICS]


class UsageReader:
    """Answers one usage metric per UTC day from ``usage_daily`` and ``usage_minute``."""

    def __init__(self, client: Any, database: str) -> None:
        if not _IDENT.fullmatch(database):
            raise ValueError(f"unsafe database identifier: {database!r}")
        self._client = client
        self._database = database
        self._queries = (yaml_load(_QUERIES) or {}).get("queries", {})

    def daily(
        self,
        metric: UsageMetric,
        *,
        scopes: Sequence[str],
        first_day: date,
        last_day: date,
    ) -> list[UsagePoint]:
        """The metric per UTC day from *first_day* to *last_day* inclusive, over *scopes*.

        A day with no row is absent rather than 0: the rollup writes a row only for
        a scope it measured.

        Args:
            metric: The metric, from :func:`usage_metric`.
            scopes: The ``_org_id`` values to read, ``[ALL_ORGS]`` for the deployment.
            first_day: The first UTC day.
            last_day: The last UTC day.

        Returns:
            One point per day that has rows, in day order.

        Raises:
            ValueError: *scopes* is empty, or *first_day* is after *last_day*.
            UsageUnavailableError: ClickHouse refused the read or did not answer.
        """
        held = sorted(set(scopes))
        if not held:
            raise ValueError("a usage read held to no scope would read nothing")
        if first_day > last_day:
            raise ValueError(f"the range starts {first_day} and ends before it, {last_day}")
        several_peaks = metric.daily == _DAILY_MAX and len(held) > 1
        name = "daily_peak_of_minutes" if several_peaks else "daily"
        rows = self._run(
            name,
            {
                "metric": metric.name,
                "scopes": held,
                "first_day": first_day,
                "last_day": last_day,
            },
        )
        return [UsagePoint(day=_as_date(day), value=float(value)) for day, value in rows]

    def _run(self, name: str, params: dict[str, Any]) -> list[tuple]:
        cfg = self._queries[name]
        sql = str(cfg["sql"]).replace("__DB__", self._database)
        timeout = int(cfg.get("timeout_seconds", 10))
        try:
            result = self._client.query(
                sql, parameters=params, settings={"max_execution_time": timeout}
            )
        except Exception as exc:
            raise UsageUnavailableError(f"usage query {name!r} failed: {exc}") from exc
        return list(result.result_rows or [])


def _as_date(value: Any) -> date:
    """A ClickHouse Date as a ``date``, from the driver's date or its ISO text."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))
