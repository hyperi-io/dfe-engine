#  Project:      dfe-engine
#  File:         hunts/execution_cost.py
#  Purpose:      Alert-volume estimate recorded on a hunt fire
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The cost estimate a hunt fire stores beside its row count.

A fire's row count is what that window wrote. The estimate is the same preview a
rule gets when it is saved: how many events each compiled rule matches over the
deployment's lookback, summed across the hunt's rules. A direct query has no
compiled condition, so its estimate stays unmeasured. A preview that cannot
finish does not fail the fire.
"""

import time
from typing import Any

from clickhouse_connect.driver.exceptions import ClickHouseError
from scalo.resilience import ServiceUnavailable

from dfe_engine.hunts.hunt_spec import HuntSpec
from dfe_engine.hunts.rule_creation_service import CostEstimate, preview_failure_reason
from dfe_engine.hunts.rule_guard import (
    VolumeMeasure,
    preview_settings,
    preview_sql,
    unmeasured_warning,
    volume_verdict,
)
from dfe_engine.settings import DetectionGuardSettings

_NO_RULE = "This hunt has no compiled rule, so its alert volume was not estimated."
_NO_ORG = "the hunt reads no org"


def measure_hunt_cost(
    client: Any, spec: HuntSpec, guard: DetectionGuardSettings
) -> tuple[int, str]:
    """Count what *spec*'s rules match over the preview window.

    Args:
        client: A ClickHouse client exposing ``query``.
        spec: The hunt that just fired. Only statements with a source and a
            detection condition are counted.
        guard: The deployment's preview bounds and the window length.

    Returns:
        The window in minutes, and the :class:`CostEstimate` as JSON.
    """
    window = guard.preview_window_minutes
    statements = [
        statement
        for statement in spec.queries
        if statement.source is not None and statement.where_clause.strip()
    ]
    if not statements:
        estimate = CostEstimate(window_minutes=window, warnings=[_NO_RULE])
        return window, estimate.model_dump_json()

    # A compiled rule always reads a fenced set of orgs, so no fence counts nothing.
    if not spec.tenant_ids:
        estimate = CostEstimate(window_minutes=window, warnings=[unmeasured_warning(_NO_ORG)])
        return window, estimate.model_dump_json()

    started = time.monotonic()
    matched = 0
    totals: dict[tuple[str, str], int] = {}
    warnings: list[str] = []
    for statement in statements:
        source_db, source_table = statement.source or ("", "")
        sql = preview_sql(
            source_db,
            source_table,
            spec.timestamp_field,
            statement.where_clause,
            window,
            tenant_ids=spec.tenant_ids,
        )
        try:
            rows = client.query(sql, settings=preview_settings(guard)).result_rows
        except ServiceUnavailable:
            warnings.append(unmeasured_warning("ClickHouse could not be reached"))
            continue
        except ClickHouseError as exc:
            reason = preview_failure_reason(exc, guard, window, f"{source_db}.{source_table}")
            warnings.append(unmeasured_warning(reason))
            continue
        if not rows:
            warnings.append(unmeasured_warning("ClickHouse returned no count"))
            continue
        total, hits = (int(value) for value in rows[0])
        totals[(source_db, source_table)] = total
        matched += hits

    duration_ms = (time.monotonic() - started) * 1000
    if not totals:
        estimate = CostEstimate(
            window_minutes=window,
            duration_ms=duration_ms,
            warnings=warnings or [unmeasured_warning("ClickHouse returned no count")],
        )
        return window, estimate.model_dump_json()

    measure = VolumeMeasure(window_minutes=window, total=sum(totals.values()), matched=matched)
    source = ", ".join(sorted(f"{db}.{table}" for db, table in totals))
    band, band_warnings = volume_verdict(measure, guard, source)
    estimate = CostEstimate(
        estimated_rows=matched,
        rows_in_window=measure.total,
        projected_per_day=measure.per_day if measure.total else None,
        match_ratio=measure.ratio,
        band=band,
        window_minutes=window,
        duration_ms=duration_ms,
        warnings=[*band_warnings, *warnings],
    )
    return window, estimate.model_dump_json()
